"""Dependency readiness, separate from the API's database health check."""
import os
import threading
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pika

from app.database import get_connection
from app.services.ai_client import resolve_model_revision
from app.services.mock_documents import load_mock_documents

SERVICES = ("worker", "outbox-relay", "recovery")


@contextmanager
def service_heartbeat(service_name):
    instance_id = uuid4()
    stopped = threading.Event()

    def beat():
        with get_connection() as conn:
            conn.execute("""
                DELETE FROM synthesis_service_heartbeats
                WHERE last_seen < NOW() - INTERVAL '1 day'
            """)
            conn.execute("""
                INSERT INTO synthesis_service_heartbeats(service_name, instance_id)
                VALUES (%s, %s) ON CONFLICT (service_name, instance_id)
                DO UPDATE SET last_seen = NOW()
            """, (service_name, instance_id))

    def run():
        while not stopped.wait(10):
            try:
                beat()
            except Exception as error:
                print(f"{service_name} heartbeat failed: {type(error).__name__}", flush=True)

    beat()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=15)


def readiness_checks():
    checks = {}
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT DISTINCT service_name FROM synthesis_service_heartbeats
                WHERE last_seen > NOW() - INTERVAL '45 seconds'
            """).fetchall()
        alive = {row[0] for row in rows}
        checks["database"] = True
        checks.update({name: name in alive for name in SERVICES})
    except Exception:
        checks["database"] = False
        checks.update({name: False for name in SERVICES})
    try:
        parameters = pika.ConnectionParameters(
            host=os.getenv("RABBITMQ_HOST", "rabbitmq"),
            credentials=pika.PlainCredentials(os.environ["RABBITMQ_USER"],
                                             os.environ["RABBITMQ_PASSWORD"]),
            socket_timeout=3, stack_timeout=5, blocked_connection_timeout=3,
        )
        with pika.BlockingConnection(parameters) as connection:
            channel = connection.channel()
            jobs = channel.queue_declare(queue="synthesis.jobs", passive=True)
            events = channel.queue_declare(queue="synthesis.document-events", passive=True)
            checks["rabbitmq"] = True
            checks["worker"] = (checks["worker"] and jobs.method.consumer_count > 0
                                and events.method.consumer_count > 0)
    except Exception:
        checks["rabbitmq"] = False
        checks["worker"] = False
    try:
        response = httpx.post(
            os.getenv("CONTENT_SERVICE_URL", "http://content-api:8000").rstrip("/")
            + "/internal/documents/access-check", timeout=3,
            headers={"Authorization": f"Bearer {os.getenv('STUDYHUB_SYNTHESIS_TOKEN', '')}",
                     "User_Name": "alice"},
            json={"documentIds": ["00000000-0000-0000-0000-000000000000"]})
        response.raise_for_status()
        checks["content"] = (bool(os.getenv("STUDYHUB_SYNTHESIS_TOKEN"))
                             and isinstance(response.json().get("documents"), list))
    except Exception:
        checks["content"] = False
    try:
        resolve_model_revision()
        checks["ollama"] = True
    except Exception:
        checks["ollama"] = False
    try:
        checks["document_provider"] = (
            os.getenv("DOCUMENT_PROVIDER", "search") == "mock"
            and os.getenv("STUDYHUB_DEV_IDENTITY") == "1"
            and bool(load_mock_documents()))
    except Exception:
        checks["document_provider"] = False
    return checks
