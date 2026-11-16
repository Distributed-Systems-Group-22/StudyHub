
import hashlib
import json

from app.services.ai_client import get_generation_config


CACHE_SCHEMA_VERSION = "version-aware-v2"


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
    instruction: str,
    generation_config: dict | None = None,
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
        "generation_config": generation_config if generation_config is not None else get_generation_config(),
    }

    return _hash_payload(payload)


def generate_cache_key(user_id: str, document_ids: list[str], instruction: str) -> str:
    """Legacy callers must supply verified versions via generate_versioned_cache_key."""
    raise ValueError("Unversioned cache keys are unsafe; use generate_versioned_cache_key")
