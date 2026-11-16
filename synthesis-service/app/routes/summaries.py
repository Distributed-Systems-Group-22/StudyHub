
import os
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, status, Header
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.database import get_connection
from app.schemas import SummaryRequest, SummaryJobResponse
from app.services.cache import generate_versioned_cache_key
from app.services.job_execution import verify_source_snapshot
from app.services.content_client import ContentServiceUnavailable
from app.services.ai_client import get_generation_config
from app.services.errors import SynthesisError


router = APIRouter(
    prefix="/api/v1/summaries",
    tags=["Summaries"]
)


# ============================================================
# Development Identity
# ============================================================

def get_development_user(user_name: str | None) -> str:
    """
    Allow explicit test identities only in development mode.

    This is NOT production authentication.
    """

    if os.getenv("STUDYHUB_DEV_IDENTITY") != "1":
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured"
        )

    if user_name is None:
        raise HTTPException(
            status_code=401,
            detail="User_Name header is required"
        )

    if user_name not in ("alice", "bob"):
        raise HTTPException(
            status_code=403,
            detail="Invalid development user"
        )

    return user_name


# ============================================================
# Cache Lookup
# ============================================================

def find_cached_job(cur, user_id, cache_key, versions):
    """
    Find a completed, valid summary with matching source versions.

    This is a normal Python function, NOT a FastAPI route.
    """

    cur.execute("""
        SELECT j.job_id, r.summary_id
        FROM synthesis_jobs j
        JOIN synthesis_results r
            ON r.job_id = j.job_id
        WHERE j.user_id = %s
          AND j.cache_key = %s
          AND j.status = 'COMPLETED'
          AND r.status = 'VALID'
        ORDER BY r.created_at DESC
    """, (user_id, cache_key))

    candidates = cur.fetchall()

    for candidate in candidates:
        cur.execute("""
            SELECT DISTINCT document_id, document_version
            FROM summary_sources
            WHERE summary_id = %s
        """, (candidate["summary_id"],))

        sources = cur.fetchall()

        if len(sources) != len(versions):
            continue

        actual = {}

        for source in sources:
            doc_id = str(source["document_id"])

            if doc_id in actual:
                break

            actual[doc_id] = source["document_version"]

        if actual == versions:
            return candidate["job_id"]

    return None


# ============================================================
# Create Summary API
# ============================================================

@router.post(
    "",
    response_model=SummaryJobResponse,
    status_code=status.HTTP_202_ACCEPTED
)
def create_summary(
    request: SummaryRequest,
    user_name: str | None = Header(
        default=None,
        alias="User_Name"
    )
):
    """
    Create or reuse a synthesis job.

    Flow:
    1. Validate development identity.
    2. Verify document access and versions.
    3. Generate version-aware cache key.
    4. Reuse a valid completed summary (Cache HIT).
    5. Reuse an existing active job (In-flight Dedup).
    6. Create a new job on Cache MISS.
    7. Insert the Outbox event in the same DB transaction.
    """

    # --------------------------------------------------------
    # Step 1: Validate user identity.
    # --------------------------------------------------------

    user_id = get_development_user(user_name)

    # --------------------------------------------------------
    # Step 2: Live permission and version verification.
    # --------------------------------------------------------

    try:
        versions = verify_source_snapshot(
            user_id=user_id,
            document_ids=request.document_ids
        )

    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail="Document access denied"
        ) from error

    except (
        ContentServiceUnavailable,
        ValueError,
        KeyError
    ) as error:
        raise HTTPException(
            status_code=503,
            detail="Document verification unavailable"
        ) from error

    # --------------------------------------------------------
    # Step 3: Generate version-aware cache key.
    # --------------------------------------------------------

    documents = [
        {
            "documentId": doc_id,
            "documentVersion": version
        }
        for doc_id, version in versions.items()
    ]

    try:
        generation_config = get_generation_config()
        cache_key = generate_versioned_cache_key(
            user_id=user_id,
            documents=documents,
            instruction=request.instruction,
            generation_config=generation_config,
        )
    except (SynthesisError, ValueError) as error:
        raise HTTPException(status_code=503, detail="Generation configuration unavailable") from error

    # --------------------------------------------------------
    # Step 4: Cache lookup and concurrent deduplication.
    # --------------------------------------------------------

    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:

                # ------------------------------------------------
                # 4A. Cache HIT: reuse a completed valid summary.
                # ------------------------------------------------

                cached_job_id = find_cached_job(
                    cur=cur,
                    user_id=user_id,
                    cache_key=cache_key,
                    versions=versions
                )

                if cached_job_id is not None:
                    return SummaryJobResponse(
                        job_id=cached_job_id,
                        status="COMPLETED",
                        cache_hit=True
                    )

                # ------------------------------------------------
                # 4B. In-flight deduplication.
                # ------------------------------------------------

                cur.execute("""
                    SELECT job_id, status
                    FROM synthesis_jobs
                    WHERE user_id = %s
                      AND cache_key = %s
                      AND status IN ('PENDING', 'RUNNING')
                    LIMIT 1
                """, (
                    user_id,
                    cache_key
                ))

                existing_job = cur.fetchone()

                if existing_job is not None:
                    return SummaryJobResponse(
                        job_id=existing_job["job_id"],
                        status=existing_job["status"],
                        cache_hit=False
                    )

                # ------------------------------------------------
                # 4C. Cache MISS: atomically create a new job.
                # ------------------------------------------------

                job_id = uuid4()
                event_id = uuid4()

                cur.execute("""
                    INSERT INTO synthesis_jobs (
                        job_id,
                        user_id,
                        cache_key,
                        document_ids,
                        instruction,
                        document_versions,
                        generation_config,
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, 'PENDING'
                    )
                    ON CONFLICT (user_id, cache_key)
                    WHERE status IN ('PENDING', 'RUNNING')
                    DO NOTHING
                    RETURNING job_id
                """, (
                    job_id,
                    user_id,
                    cache_key,
                    Jsonb(request.document_ids),
                    request.instruction,
                    Jsonb(versions),
                    Jsonb(generation_config),
                ))

                inserted = cur.fetchone()

                # ------------------------------------------------
                # 4D. Another request won the insert race.
                # ------------------------------------------------

                if inserted is None:

                    cur.execute("""
                        SELECT job_id, status
                        FROM synthesis_jobs
                        WHERE user_id = %s
                          AND cache_key = %s
                          AND status IN ('PENDING', 'RUNNING')
                        LIMIT 1
                    """, (
                        user_id,
                        cache_key
                    ))

                    existing_job = cur.fetchone()

                    if existing_job is not None:
                        return SummaryJobResponse(
                            job_id=existing_job["job_id"],
                            status=existing_job["status"],
                            cache_hit=False
                        )

                    # The competing job may have completed
                    # while this request was waiting.
                    cached_job_id = find_cached_job(
                        cur=cur,
                        user_id=user_id,
                        cache_key=cache_key,
                        versions=versions
                    )

                    if cached_job_id is not None:
                        return SummaryJobResponse(
                            job_id=cached_job_id,
                            status="COMPLETED",
                            cache_hit=True
                        )

                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "Concurrent job state changed; "
                            "retry request"
                        )
                    )

                # ------------------------------------------------
                # Step 5: Transactional Outbox.
                #
                # Only the request that successfully inserted
                # the new job will create an Outbox event.
                # ------------------------------------------------

                event_payload = {
                    "event_id": str(event_id),
                    "event_type": "synthesis.requested",
                    "job_id": str(job_id),
                    "user_id": user_id,
                    "document_ids": request.document_ids,
                    "instruction": request.instruction,
                    "cache_key": cache_key
                }

                cur.execute("""
                    INSERT INTO outbox_events (
                        event_id,
                        event_type,
                        aggregate_id,
                        payload
                    )
                    VALUES (%s, %s, %s, %s)
                """, (
                    event_id,
                    "synthesis.requested",
                    job_id,
                    Jsonb(event_payload)
                ))

        # --------------------------------------------------------
        # Step 6: Return the newly created job.
        # --------------------------------------------------------

        return SummaryJobResponse(
            job_id=job_id,
            status="PENDING",
            cache_hit=False
        )

    except psycopg.Error as error:
        raise HTTPException(
            status_code=503,
            detail="Unable to process summary request"
        ) from error
