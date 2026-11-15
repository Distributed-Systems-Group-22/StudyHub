
import hashlib
import json

from app.services.ai_client import (
    AI_MODEL,
    PROMPT_VERSION,
)


# Temporary contract until Search Service exposes
# a stable retrieval/index version.
RETRIEVAL_VERSION = "retrieval-v1"

CACHE_SCHEMA_VERSION = "version-aware-v1"


def _hash_payload(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def generate_versioned_cache_key(
    user_id: str,
    documents: list[dict],
    instruction: str
) -> str:
    """
    Generate a cache key using verified document versions.

    Expected documents:
    [
        {
            "documentId": "...",
            "documentVersion": 1
        }
    ]
    """

    if not user_id or not user_id.strip():
        raise ValueError("user_id is required")

    if not documents:
        raise ValueError("At least one document is required")

    if not instruction or not instruction.strip():
        raise ValueError("instruction is required")

    normalized_documents = []
    seen_ids = set()

    for doc in documents:
        if not isinstance(doc, dict):
            raise ValueError("Invalid document metadata")

        doc_id = doc.get("documentId")
        version = doc.get("documentVersion")

        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError("Invalid document ID")

        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise ValueError("Invalid document version")

        doc_id = doc_id.strip()

        if doc_id in seen_ids:
            raise ValueError("Duplicate document ID")

        seen_ids.add(doc_id)

        normalized_documents.append({
            "document_id": doc_id,
            "document_version": version
        })

    normalized_documents.sort(
        key=lambda doc: doc["document_id"]
    )

    payload = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "user_id": user_id.strip(),
        "documents": normalized_documents,
        "instruction": instruction.strip(),
        "model_version": AI_MODEL,
        "prompt_version": PROMPT_VERSION,
        "retrieval_version": RETRIEVAL_VERSION
    }

    return _hash_payload(payload)


# Legacy function retained temporarily so existing
# job creation continues working during Stage 4A.
# Do NOT use these keys for cache hits.
def generate_cache_key(
    user_id: str,
    document_ids: list[str],
    instruction: str
) -> str:
    payload = {
        "user_id": user_id,
        "documents": sorted(document_ids),
        "instruction": instruction.strip(),
        "model_version": "mock-model-v1",
        "prompt_version": "prompt-v1",
        "retrieval_version": RETRIEVAL_VERSION
    }

    return _hash_payload(payload)
