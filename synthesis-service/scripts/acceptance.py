"""Real local acceptance test. Temporarily stops development workers and dependencies.

Requires Docker Compose, the running development stack, Ollama and explicit mock mode.
Only synthetic documents/jobs created by this run are changed. Existing jobs must be idle.
"""
import concurrent.futures
import json
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import httpx

from prepare_demo import DEMO_TEXTS, FIXTURE, prepare_documents

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "synthesis-service" / "acceptance-results.json"
API = "http://127.0.0.1:8002"
CONTENT = "http://127.0.0.1:8000"
HEADERS = {"User_Name": "alice"}
evidence = []


def compose(*args):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def inside(code, value=None, service="synthesis-api"):
    output = compose("exec", "-T", service, "python", "-c", code, json.dumps(value))
    return json.loads(output)


def sql(statement, parameters=(), service="synthesis-api"):
    return inside("""import json, os, sys, psycopg
from psycopg.rows import dict_row
query, parameters = json.loads(sys.argv[1])
key = 'STUDYHUB_DATABASE_URL' if 'STUDYHUB_DATABASE_URL' in os.environ else 'DATABASE_URL'
with psycopg.connect(os.environ[key], row_factory=dict_row) as conn:
    cursor = conn.execute(query, parameters)
    rows = cursor.fetchall() if cursor.description else []
print(json.dumps(rows, default=str))
""", [statement, parameters], service)


def wait_for(check, description, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise AssertionError(f"Timed out: {description}")


def record(name):
    evidence.append({"check": name, "status": "PASS"})
    print(f"PASS: {name}", flush=True)


def request(method, path, **kwargs):
    with httpx.Client(base_url=API, headers=HEADERS, timeout=30) as client:
        return client.request(method, path, **kwargs)


def submit(ids, instruction):
    response = request("POST", "/api/v1/summaries", json={
        "document_ids": ids, "instruction": instruction})
    response.raise_for_status()
    assert response.status_code == 202
    return response.json()


def status(job_id):
    response = request("GET", f"/api/v1/summaries/jobs/{job_id}")
    response.raise_for_status()
    return response.json()


def completed(job_id, timeout=300):
    def check():
        job = status(job_id)
        assert job["status"] != "FAILED", job
        return job if job["status"] == "COMPLETED" else None
    return wait_for(check, f"job {job_id} completion", timeout)


def grounded(result, documents):
    citations = {c["source_id"]: c for c in result["citations"]}
    referenced = set()
    for paragraph in result["summary_text"].split("\n\n"):
        match = re.fullmatch(r"> (.+) \[(S\d+)\]", paragraph)
        assert match, paragraph
        text, label = match.groups()
        citation = citations[label]
        assert text in documents[citation["document_id"]]["content"]
        assert citation["document_version"] == documents[citation["document_id"]]["version"]
        referenced.add(citation["document_id"])
    assert referenced == set(documents)


def ready():
    return request("GET", "/ready").status_code == 200


def main():
    mode = inside("import os,json; print(json.dumps({k:os.getenv(k) for k in ['DOCUMENT_PROVIDER','STUDYHUB_DEV_IDENTITY','MOCK_DOCUMENTS_FILE']}))")
    assert mode == {"DOCUMENT_PROVIDER": "mock", "STUDYHUB_DEV_IDENTITY": "1",
                    "MOCK_DOCUMENTS_FILE": "/app/dev-fixtures/documents.json"}, mode
    active = sql("SELECT job_id FROM synthesis_jobs WHERE status IN ('PENDING','RUNNING')")
    assert not active, "Wait for existing jobs to finish before fault tests"
    run = uuid4().hex[:8]
    texts = DEMO_TEXTS * 3 + [" ".join(f"Scenario {i}: {text}" for i in range(12)) for text in DEMO_TEXTS]
    documents = prepare_documents(texts)
    ids = list(documents)
    wait_for(ready, "all dependencies ready")
    record("dependency readiness with real Content, PostgreSQL, RabbitMQ and Ollama")

    assert request("POST", "/api/v1/summaries", json={"document_ids": []}).status_code == 422
    assert request("POST", "/api/v1/summaries", headers={"User_Name": "bob"},
                   json={"document_ids": ids[:2]}).status_code == 403
    record("HTTP validation and Content document access enforcement")

    instruction = f"Select the main points about reliable messaging from both documents. Test {run}."
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda _: submit(ids[:2], instruction), range(6)))
    assert len({job["job_id"] for job in jobs}) == 1, jobs
    job_id = jobs[0]["job_id"]
    result = completed(job_id)
    grounded(result, {key: documents[key] for key in ids[:2]})
    assert request("GET", f"/api/v1/summaries/jobs/{job_id}",
                   headers={"User_Name": "bob"}).status_code == 404
    hit = submit(list(reversed(ids[:2])), instruction)
    assert hit["job_id"] == job_id and hit["cache_hit"] is True
    assert len(sql("SELECT summary_id FROM synthesis_results WHERE job_id=%s", [job_id])) == 1
    assert sql("SELECT published_at FROM outbox_events WHERE aggregate_id=%s", [job_id])[0]["published_at"]
    record("six concurrent requests deduplicated; real inference, grounded citations and cache reuse")

    # Duplicate broker delivery must not generate another result.
    event = {"event_type": "synthesis.requested", "job_id": job_id}
    sql("INSERT INTO outbox_events(event_id,event_type,aggregate_id,payload) VALUES(%s,%s,%s,%s::jsonb)",
        [str(uuid4()), "synthesis.requested", job_id, json.dumps(event)])
    wait_for(lambda: not sql("SELECT event_id FROM outbox_events WHERE aggregate_id=%s AND published_at IS NULL", [job_id]), "duplicate publication")
    time.sleep(3)
    assert len(sql("SELECT summary_id FROM synthesis_results WHERE job_id=%s", [job_id])) == 1
    record("duplicate real RabbitMQ delivery does not duplicate a completed result")

    long_job = submit(ids[6:8], f"Select reliable messaging facts from every document. Long test {run}.")
    long_result = completed(long_job["job_id"])
    grounded(long_result, {key: documents[key] for key in ids[6:8]})
    record("real long-document hierarchical excerpt selection with both source documents")

    deleted_job = submit([ids[2]], f"Select key facts. Deletion test {run}.")
    completed(deleted_job["job_id"])
    with httpx.Client(base_url=CONTENT, headers=HEADERS) as client:
        response = client.delete(f"/documents/{ids[2]}")
        assert response.status_code == 204
    assert request("GET", f"/api/v1/summaries/jobs/{deleted_job['job_id']}").status_code in (404, 409)
    wait_for(lambda: sql("SELECT status FROM synthesis_results WHERE job_id=%s", [deleted_job["job_id"]])[0]["status"] == "INVALIDATED", "deletion invalidation")
    record("real Content deletion event invalidates stored summary and blocks retrieval")

    compose("stop", "synthesis-worker")
    assert request("GET", "/health").status_code == 200
    assert request("GET", "/ready").json()["checks"]["worker"] is False
    queued = submit([ids[3]], f"Version test {run}.")
    sql("UPDATE documents SET version=version+1 WHERE id=%s", [ids[3]], service="content-api")
    compose("start", "synthesis-worker")
    wait_for(lambda: status(queued["job_id"])["status"] == "FAILED", "source version rejection")
    assert "source_version_expired" in status(queued["job_id"])["error_message"]
    record("queued source-version changes rejected before generation")

    compose("stop", "synthesis-worker")
    retry = submit([ids[4]], f"Retry test {run}.")
    inside("""import json,sys
from app.services.job_execution import claim_job, fail_job
from app.services.errors import ProviderUnavailable
job=claim_job(json.loads(sys.argv[1])); assert job
fail_job(job, ProviderUnavailable('Acceptance-test temporary outage'))
print(json.dumps(True))
""", retry["job_id"])
    pending = status(retry["job_id"])
    assert pending["status"] == "PENDING" and pending["attempts"] == 1
    assert sql("SELECT available_at > created_at AS delayed FROM outbox_events WHERE aggregate_id=%s ORDER BY created_at DESC LIMIT 1", [retry["job_id"]])[0]["delayed"]
    compose("start", "synthesis-worker")
    assert completed(retry["job_id"])["attempts"] == 2
    record("transient failure persists delayed retry; real worker succeeds on attempt two")

    crash = submit([ids[5]], f"Crash recovery test {run}.")
    wait_for(lambda: status(crash["job_id"])["status"] == "RUNNING", "worker claim")
    old_claim = sql("SELECT claim_token FROM synthesis_jobs WHERE job_id=%s", [crash["job_id"]])[0]["claim_token"]
    compose("kill", "-s", "SIGKILL", "synthesis-worker")
    print("Waiting for the real 45-second lease to expire...", flush=True)
    wait_for(lambda: status(crash["job_id"])["status"] == "PENDING", "lease recovery after actual worker kill", timeout=90)
    compose("start", "synthesis-worker")
    assert completed(crash["job_id"])["attempts"] == 2
    stale = inside("""import json,sys
from app.services.job_execution import renew_lease
job_id,token=json.loads(sys.argv[1])
print(json.dumps(renew_lease({'job_id':job_id,'claim_token':token})))
""", [crash["job_id"], old_claim])
    assert stale is False
    committed = inside("""import json,sys
from psycopg.rows import dict_row
from app.database import get_connection
from app.services.job_execution import complete_job
job_id,token=json.loads(sys.argv[1])
with get_connection() as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT * FROM synthesis_jobs WHERE job_id=%s',(job_id,)); job=cur.fetchone()
job['claim_token']=token
config=job['generation_config']
print(json.dumps(complete_job(job,'stale output',config['model_revision'],config['prompt_version'],[],[])))
""", [crash["job_id"], old_claim])
    assert committed is False
    assert len(sql("SELECT summary_id FROM synthesis_results WHERE job_id=%s", [crash["job_id"]])) == 1
    record("actual worker SIGKILL, lease expiry, recovery and stale-claim fencing")

    compose("stop", "synthesis-worker")
    permanent = submit([ids[0]], f"Permanent failure test {run}.")
    exhausted = submit([ids[0]], f"Retry exhaustion test {run}.")
    inside("""import json,sys
from app.database import get_connection
from app.services.job_execution import claim_job, fail_job
from app.services.errors import InvalidCitation, ProviderUnavailable
permanent,exhausted=json.loads(sys.argv[1])
fail_job(claim_job(permanent), InvalidCitation('Acceptance-test invalid selection'))
for attempt in range(3):
    with get_connection() as conn:
        conn.execute("UPDATE synthesis_jobs SET next_attempt_at=NOW() WHERE job_id=%s", (exhausted,))
    job=claim_job(exhausted); assert job
    fail_job(job, ProviderUnavailable('Acceptance-test persistent outage'))
print(json.dumps(True))
""", [permanent["job_id"], exhausted["job_id"]])
    assert status(permanent["job_id"])["status"] == "FAILED"
    assert status(permanent["job_id"])["attempts"] == 1
    assert status(exhausted["job_id"])["status"] == "FAILED"
    assert status(exhausted["job_id"])["attempts"] == 3
    record("permanent errors fail once; persistent transient errors stop after three attempts")

    changed = submit([ids[0]], f"Configuration change test {run}.")
    original = FIXTURE.read_text(encoding="utf-8")
    altered = json.loads(original)
    altered[ids[0]]["content"] += " Changed acceptance fixture."
    FIXTURE.write_text(json.dumps(altered), encoding="utf-8")
    try:
        compose("start", "synthesis-worker")
        wait_for(lambda: status(changed["job_id"])["status"] == "FAILED", "configuration change rejection")
        assert "generation_config_changed" in status(changed["job_id"])["error_message"]
    finally:
        FIXTURE.write_text(original, encoding="utf-8")
    record("queued generation/provider configuration changes rejected")

    for service, check in [("synthesis-outbox-relay", "outbox-relay"),
                           ("synthesis-recovery", "recovery")]:
        compose("stop", service)
        try:
            # Expire only the stopped process's heartbeat rather than waiting 45 seconds.
            sql("UPDATE synthesis_service_heartbeats SET last_seen=NOW()-INTERVAL '60 seconds' WHERE service_name=%s", [check])
            assert request("GET", "/ready").json()["checks"][check] is False
        finally:
            compose("start", service)
        wait_for(ready, f"{check} heartbeat restoration")
        record(f"readiness detects stopped {check} and recovers after restart")

    for service, check in [("content-api", "content"), ("rabbitmq", "rabbitmq"),
                           ("synthesis-db", "database")]:
        compose("stop", service)
        try:
            assert request("GET", "/ready").json()["checks"][check] is False
            if service == "content-api":
                response = request("GET", f"/api/v1/summaries/jobs/{job_id}")
                assert response.status_code == 503 and "summary_text" not in response.json()
            if service == "synthesis-db":
                assert request("GET", "/health").status_code == 503
        finally:
            compose("start", service)
        wait_for(ready, f"readiness after {service} restart")
        record(f"{service} outage detected; readiness recovers after restart")

    checks = inside("""import json,os
from app.services.readiness import readiness_checks
os.environ['STUDYHUB_SYNTHESIS_TOKEN']='deliberately-invalid-test-token'
print(json.dumps(readiness_checks()))
""")
    assert checks["content"] is False and checks["database"] is True
    record("readiness rejects an invalid Content service token without changing live configuration")

    assert ready()
    evidence.append({"demo_document_ids": ids[:2], "demo_job_id": job_id,
                     "summary_text": result["summary_text"]})


if __name__ == "__main__":
    started = time.monotonic()
    try:
        main()
    except Exception as error:
        evidence.append({"status": "FAIL", "error": str(error)})
        raise
    finally:
        # Restore services even if a fault assertion failed.
        compose("start", "content-api", "rabbitmq", "synthesis-db", "synthesis-worker",
                "synthesis-outbox-relay", "synthesis-recovery")
        RESULTS.write_text(json.dumps({"elapsed_seconds": round(time.monotonic()-started, 1),
                                       "results": evidence}, indent=2), encoding="utf-8")
