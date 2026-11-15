from app.routes.summaries import get_development_user

from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, Header

from app.database import get_connection
from app.schemas import JobStatusResponse
from app.services.content_client import (
    verify_document_access,
    ContentServiceUnavailable,
)


router = APIRouter(
    prefix="/api/v1/summaries/jobs",
    tags=["Jobs"]
)


def sources_are_current(sources, verified_docs):
    """Check that all source versions remain accessible."""

    if not sources:
        return False

    expected = {}

    for source in sources:
        doc_id = str(source["document_id"])
        version = source["document_version"]

        if doc_id in expected and expected[doc_id] != version:
            return False

        expected[doc_id] = version

    actual = {}

    for doc in verified_docs:
        if (
            "documentId" not in doc
            or "documentVersion" not in doc
        ):
            return False

        doc_id = str(doc["documentId"])
        version = doc["documentVersion"]

        if doc_id in actual:
            return False

        actual[doc_id] = version

    return expected == actual


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse
)
def get_job_status(
    job_id: UUID,
    user_name: str | None = Header(
        default=None,
        alias="User_Name"
    )
):
    # Development identity.
    # Authentication hardening is handled in the next step.
    user_id = get_development_user(user_name)

    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT
                        j.job_id,
                        j.status,
                        j.attempts,
                        j.error_message,
                        r.summary_id,
                        r.summary_text,
                        r.status AS summary_status
                    FROM synthesis_jobs j
                    LEFT JOIN synthesis_results r
                        ON j.job_id = r.job_id
                    WHERE j.job_id = %s
                      AND j.user_id = %s
                """, (job_id, user_id))

                job = cur.fetchone()

                if job is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Job not found"
                    )

                # No summary should be exposed until completion.
                if job["status"] != "COMPLETED":
                    job["summary_text"] = None
                    return JobStatusResponse(**job)

                # Fail closed if the result is missing or invalidated.
                if (
                    job["summary_id"] is None
                    or job["summary_status"] != "VALID"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Summary is unavailable or invalidated"
                    )

                cur.execute("""
                    SELECT
                        document_id,
                        document_version
                    FROM summary_sources
                    WHERE summary_id = %s
                """, (job["summary_id"],))

                sources = cur.fetchall()

    except psycopg.Error as error:
        raise HTTPException(
            status_code=503,
            detail="Synthesis database unavailable"
        ) from error

    # A completed summary must have recorded sources.
    if not sources:
        raise HTTPException(
            status_code=409,
            detail="Summary source metadata is missing"
        )

    document_ids = list({
        str(source["document_id"])
        for source in sources
    })

    try:
        verified_docs = verify_document_access(
            user_id=user_id,
            document_ids=document_ids
        )

    except ContentServiceUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail="Document access verification unavailable"
        ) from error

    if not sources_are_current(sources, verified_docs):
        raise HTTPException(
            status_code=404,
            detail="Summary is no longer accessible"
        )

    # Recheck the stored summary status after the external API call.
    try:
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT
                        r.summary_text,
                        r.status AS summary_status
                    FROM synthesis_results r
                    JOIN synthesis_jobs j
                        ON j.job_id = r.job_id
                    WHERE r.summary_id = %s
                      AND j.job_id = %s
                      AND j.user_id = %s
                      AND j.status = 'COMPLETED'
                """, (
                    job["summary_id"],
                    job_id,
                    user_id
                ))

                current_result = cur.fetchone()

    except psycopg.Error as error:
        raise HTTPException(
            status_code=503,
            detail="Synthesis database unavailable"
        ) from error

    if (
        current_result is None
        or current_result["summary_status"] != "VALID"
    ):
        raise HTTPException(
            status_code=409,
            detail="Summary is unavailable or invalidated"
        )

    job["summary_text"] = current_result["summary_text"]
    job["summary_status"] = current_result["summary_status"]

    return JobStatusResponse(**job)
