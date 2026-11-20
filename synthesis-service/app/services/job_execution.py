import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.database import get_connection
from app.services.ai_client import generate_summary, ensure_generation_config
from app.services.cache import generate_versioned_cache_key
from app.services.content_client import verify_document_access, ContentServiceUnavailable
from app.services.document_provider import get_document_chunks
from app.services.errors import (
    BudgetExceeded, InvalidSourceData, LeaseLost, SourceVersionExpired, SynthesisError,
)

MAX_ATTEMPTS = 3
LEASE_SECONDS = 45
MAX_JOB_SECONDS = int(os.getenv("AI_MAX_JOB_SECONDS", "900"))
if MAX_JOB_SECONDS < LEASE_SECONDS:
    raise ValueError("AI_MAX_JOB_SECONDS must be at least LEASE_SECONDS")


def claim_job(job_id):
    """Atomically claim a pending job; no payload is trusted from RabbitMQ."""
    token = uuid4()
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM synthesis_jobs WHERE job_id = %s FOR UPDATE", (job_id,))
            job = cur.fetchone()
            if job is None or job["status"] != "PENDING":
                return None
            if job["next_attempt_at"] > datetime.now(timezone.utc):
                return None
            if (job["attempts"] >= MAX_ATTEMPTS or not job["document_ids"]
                    or not job["instruction"] or not job["document_versions"]
                    or not job["generation_config"]):
                cur.execute("""
                    UPDATE synthesis_jobs SET status = 'FAILED',
                        error_message = 'Missing safe job snapshot or maximum attempts reached',
                        updated_at = NOW() WHERE job_id = %s
                """, (job_id,))
                return None
            cur.execute("""
                UPDATE synthesis_jobs SET status = 'RUNNING', attempts = attempts + 1,
                    claim_token = %s, lease_until = NOW() + %s * INTERVAL '1 second',
                    updated_at = NOW() WHERE job_id = %s
            """, (token, LEASE_SECONDS, job_id))
            return {**job, "claim_token": token, "attempt": job["attempts"] + 1}


def renew_lease(job):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE synthesis_jobs
                SET lease_until = NOW() + %s * INTERVAL '1 second', updated_at = NOW()
                WHERE job_id = %s AND status = 'RUNNING' AND claim_token = %s
                  AND lease_until > NOW()
                RETURNING job_id
            """, (LEASE_SECONDS, job["job_id"], job["claim_token"]))
            return cur.fetchone() is not None


@contextmanager
def job_lease(job, cancelled):
    stopped, lost = threading.Event(), threading.Event()
    deadline = time.monotonic() + MAX_JOB_SECONDS

    def checkpoint():
        if cancelled.is_set() or lost.is_set():
            raise LeaseLost("Worker no longer owns a live delivery/lease")
        if time.monotonic() >= deadline:
            raise BudgetExceeded("Maximum job execution time reached")

    def heartbeat():
        while not stopped.wait(LEASE_SECONDS / 3):
            if cancelled.is_set() or time.monotonic() >= deadline:
                return
            try:
                if not renew_lease(job):
                    lost.set()
                    return
            except Exception as error:
                print(f"Lease renewal failed for {job['job_id']}: {type(error).__name__}", flush=True)
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        checkpoint()
        yield checkpoint
    finally:
        stopped.set()
        thread.join(timeout=15)


def lock_source_documents(cur, document_ids):
    # Same transaction locks are used by deletion handling to serialize insertion.
    for doc_id in sorted(document_ids):
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (doc_id,))


def complete_job(job, summary_text, model_version, prompt_version, sources, citations):
    """Commit result, all source dependencies and verified citations atomically."""
    if (model_version != job["generation_config"]["model_revision"]
            or prompt_version != job["generation_config"]["prompt_version"]):
        raise InvalidSourceData("Result configuration differs from the job snapshot")
    summary_id = uuid4()
    with get_connection() as conn:
        with conn.cursor() as cur:
            lock_source_documents(cur, job["document_ids"])
            cur.execute("SELECT document_id FROM synthesis_deleted_documents WHERE document_id = ANY(%s)",
                        (job["document_ids"],))
            if cur.fetchone():
                raise PermissionError("permission_revoked: source was deleted")
            cur.execute("""
                UPDATE synthesis_jobs SET status = 'COMPLETED', completed_at = NOW(),
                    lease_until = NULL, claim_token = NULL, error_message = NULL, updated_at = NOW()
                WHERE job_id = %s AND status = 'RUNNING' AND claim_token = %s AND lease_until > NOW()
                RETURNING job_id
            """, (job["job_id"], job["claim_token"]))
            if cur.fetchone() is None:
                return False
            cur.execute("""
                INSERT INTO synthesis_results
                    (summary_id, job_id, cache_key, summary_text, model_version, prompt_version, citations, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'VALID')
            """, (summary_id, job["job_id"], job["cache_key"], summary_text,
                  model_version, prompt_version, Jsonb(citations)))
            # Record all inputs, including uncited ones, for permissions/invalidation.
            for chunk in sources:
                cur.execute("""
                    INSERT INTO summary_sources
                        (summary_id, document_id, document_version, chunk_id, page, source_location)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (summary_id, chunk.document_id, chunk.document_version,
                      chunk.chunk_id, chunk.page, chunk.source_location))
    return True


def fail_job(job, error):
    """Permanent errors fail immediately; transient errors keep bounded backoff."""
    permanent = (isinstance(error, PermissionError)
                 or isinstance(error, SynthesisError) and not error.retryable)
    message = str(error)[:500] if isinstance(error, (SynthesisError, PermissionError)) else f"infrastructure_error: {type(error).__name__}"
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT status, claim_token, attempts, lease_until
                FROM synthesis_jobs WHERE job_id = %s FOR UPDATE
            """, (job["job_id"],))
            current = cur.fetchone()
            if (current is None or current["status"] != "RUNNING"
                    or current["claim_token"] != job["claim_token"]
                    or current["lease_until"] <= datetime.now(timezone.utc)):
                return
            if permanent or current["attempts"] >= MAX_ATTEMPTS:
                cur.execute("""
                    UPDATE synthesis_jobs SET status = 'FAILED', claim_token = NULL,
                        lease_until = NULL, error_message = %s, updated_at = NOW()
                    WHERE job_id = %s
                """, (message, job["job_id"]))
                return
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=2 ** current["attempts"])
            cur.execute("""
                UPDATE synthesis_jobs SET status = 'PENDING', claim_token = NULL, lease_until = NULL,
                    next_attempt_at = %s, error_message = %s, updated_at = NOW() WHERE job_id = %s
            """, (retry_at, message, job["job_id"]))
            event_id = uuid4()
            payload = {"event_id": str(event_id), "event_type": "synthesis.requested", "job_id": str(job["job_id"])}
            cur.execute("""
                INSERT INTO outbox_events (event_id, event_type, aggregate_id, payload, available_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (event_id, "synthesis.requested", job["job_id"], Jsonb(payload), retry_at))


def verify_source_snapshot(user_id, document_ids):
    verified_docs = verify_document_access(user_id=user_id, document_ids=document_ids)
    requested = set(document_ids)
    verified = {}
    for doc in verified_docs:
        if not isinstance(doc, dict):
            raise ContentServiceUnavailable("Malformed access response")
        doc_id, version = doc.get("documentId"), doc.get("documentVersion")
        if (not isinstance(doc_id, str) or doc_id not in requested or doc_id in verified
                or type(version) is not int or version < 1):
            raise ContentServiceUnavailable("Invalid source metadata in access response")
        verified[doc_id] = version
    if set(verified) != requested:
        raise PermissionError("permission_revoked: one or more documents are missing or unauthorized")
    return verified


def validate_job_snapshot(job):
    versions = job.get("document_versions")
    if (not isinstance(versions, dict) or set(versions) != set(job["document_ids"])
            or len(job["document_ids"]) != len(versions)
            or any(type(v) is not int or v < 1 for v in versions.values())
            or not isinstance(job.get("generation_config"), dict)):
        raise InvalidSourceData("Legacy or malformed job snapshot; resubmit request")
    ensure_generation_config(job["generation_config"])
    key = generate_versioned_cache_key(
        job["user_id"], [{"documentId": d, "documentVersion": v} for d, v in versions.items()],
        job["instruction"], job["generation_config"])
    if key != job["cache_key"]:
        raise InvalidSourceData("Job snapshot does not match its cache key")


def process_job(job_id, cancelled=None):
    job = claim_job(job_id)
    if job is None:
        return
    cancelled = cancelled if cancelled is not None else threading.Event()
    try:
        with job_lease(job, cancelled) as checkpoint:
            validate_job_snapshot(job)
            versions = job["document_versions"]
            if verify_source_snapshot(job["user_id"], job["document_ids"]) != versions:
                raise SourceVersionExpired("Sources changed while this job was queued; resubmit")
            chunks = get_document_chunks(job["document_ids"], versions, user_id=job["user_id"])
            checkpoint()
            result = generate_summary(chunks, job["instruction"], job["generation_config"], checkpoint)
            ensure_generation_config(job["generation_config"])
            if verify_source_snapshot(job["user_id"], job["document_ids"]) != versions:
                raise SourceVersionExpired("Sources changed during generation; resubmit")
            checkpoint()
            completed = complete_job(
                job, result["summary_text"], result["model_version"], result["prompt_version"],
                chunks, result["citations"])
            print(f"Job {job_id}: {'completed' if completed else 'stale result discarded'}", flush=True)
    except LeaseLost as error:
        # Stop renewing; Recovery will retry if this worker lost its delivery/lease.
        print(f"Job {job_id}: {error}", flush=True)
    except Exception as error:
        print(f"Job {job_id} failed: {type(error).__name__}", flush=True)
        fail_job(job, error)
