
import os

import httpx


CONTENT_SERVICE_URL = os.getenv(
    "CONTENT_SERVICE_URL",
    "http://content-api:8000"
)

SYNTHESIS_TOKEN = os.getenv(
    "STUDYHUB_SYNTHESIS_TOKEN",
    ""
)


class ContentServiceUnavailable(Exception):
    """Content Service cannot verify document access."""


def verify_document_access(
    user_id: str,
    document_ids: list[str]
) -> list[dict]:
    """
    Verify document permissions and versions with Content Service.

    Fail-closed policy:
    - Missing configuration -> raise error
    - Service unavailable -> raise error
    - Unauthorized documents -> not included in response
    """

    if not document_ids:
        return []

    # Never bypass permission checks.
    if not SYNTHESIS_TOKEN:
        raise ContentServiceUnavailable(
            "STUDYHUB_SYNTHESIS_TOKEN is not configured"
        )

    if not CONTENT_SERVICE_URL:
        raise ContentServiceUnavailable(
            "CONTENT_SERVICE_URL is not configured"
        )

    url = (
        f"{CONTENT_SERVICE_URL.rstrip('/')}"
        "/internal/documents/access-check"
    )

    headers = {
        "Authorization": f"Bearer {SYNTHESIS_TOKEN}",
        "User_Name": user_id,
        "Content-Type": "application/json",
    }

    payload = {
        "documentIds": document_ids
    }

    try:
        with httpx.Client(timeout=4.0) as client:
            response = client.post(
                url,
                json=payload,
                headers=headers
            )

    except httpx.RequestError as error:
        raise ContentServiceUnavailable(
            "Content Service is unreachable"
        ) from error

    if response.status_code != 200:
        raise ContentServiceUnavailable(
            f"Content Service verification failed: "
            f"HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except ValueError as error:
        raise ContentServiceUnavailable(
            "Content Service returned invalid JSON"
        ) from error

    if not isinstance(data, dict):
        raise ContentServiceUnavailable("Content Service returned a non-object response")
    documents = data.get("documents")

    if not isinstance(documents, list):
        raise ContentServiceUnavailable(
            "Content Service returned invalid document data"
        )

    return documents
