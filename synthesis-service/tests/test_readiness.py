import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import httpx
import psycopg

from app.main import app
from app.services import readiness
from tests.support import SynthesisTestCase


class ReadinessTests(SynthesisTestCase):
    def setUp(self):
        super().setUp()
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.connection.execute.return_value.fetchall.return_value = [(name,) for name in readiness.SERVICES]
        self.database_connection = self.enterContext(patch.object(readiness, "get_connection", return_value=self.connection))
        self.broker = MagicMock()
        self.broker.__enter__.return_value = self.broker
        self.channel = self.broker.channel.return_value
        self.channel.queue_declare.return_value.method.consumer_count = 1
        self.broker_factory = self.enterContext(patch.object(readiness.pika, "BlockingConnection", return_value=self.broker))
        self.content = self.enterContext(patch.object(readiness.httpx, "post", return_value=httpx.Response(
            200, json={"documents": []}, request=httpx.Request("POST", "http://content.invalid"))))
        self.model = self.enterContext(patch.object(readiness, "resolve_model_revision", return_value="digest-v1"))

    def test_ready_requires_all_dependencies_and_processes(self):
        self.assertTrue(all(readiness.readiness_checks().values()))
        headers = self.content.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer unit-test-token")

    def test_database_failure_also_marks_processes_unavailable(self):
        self.database_connection.side_effect = psycopg.OperationalError("offline")
        result = readiness.readiness_checks()
        self.assertFalse(result["database"])
        self.assertTrue(all(not result[name] for name in readiness.SERVICES))

    def test_expired_relay_or_recovery_heartbeat_is_not_ready(self):
        for missing in ("outbox-relay", "recovery"):
            with self.subTest(missing=missing):
                self.connection.execute.return_value.fetchall.return_value = [(s,) for s in readiness.SERVICES if s != missing]
                self.assertFalse(readiness.readiness_checks()[missing])

    def test_worker_heartbeat_alone_is_not_enough_without_consumers(self):
        self.channel.queue_declare.return_value.method.consumer_count = 0
        self.assertFalse(readiness.readiness_checks()["worker"])

    def test_broker_failure_marks_worker_unavailable(self):
        self.broker_factory.side_effect = RuntimeError("offline")
        result = readiness.readiness_checks()
        self.assertFalse(result["rabbitmq"])
        self.assertFalse(result["worker"])

    def test_content_token_or_outage_failure_is_not_ready(self):
        self.content.return_value = httpx.Response(401, request=httpx.Request("POST", "http://content.invalid"))
        self.assertFalse(readiness.readiness_checks()["content"])

    def test_model_unavailable_does_not_hide_provider_status(self):
        self.model.side_effect = RuntimeError("offline")
        result = readiness.readiness_checks()
        self.assertFalse(result["ollama"])
        self.assertTrue(result["document_provider"])

    def test_pending_search_is_explicitly_not_ready(self):
        with patch.dict(os.environ, {"DOCUMENT_PROVIDER": "search"}):
            self.assertFalse(readiness.readiness_checks()["document_provider"])

    def test_heartbeat_registers_instance_and_stops_thread_on_exit(self):
        with patch.object(readiness.threading, "Thread") as thread:
            with readiness.service_heartbeat("worker"):
                calls = self.connection.execute.call_args_list
                self.assertTrue(any("INSERT INTO synthesis_service_heartbeats" in c.args[0] for c in calls))
            thread.return_value.join.assert_called_once()

    def test_ready_endpoint_reports_200_or_503_with_checks(self):
        client = TestClient(app)
        self.addCleanup(client.close)
        for available, status in [(True, 200), (False, 503)]:
            with self.subTest(available=available), patch("app.main.readiness_checks", return_value={"worker": available}):
                response = client.get("/ready")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["checks"], {"worker": available})

    def test_health_database_failure_is_503(self):
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch("app.main.get_connection", side_effect=psycopg.OperationalError("offline")):
            self.assertEqual(client.get("/health").status_code, 503)
