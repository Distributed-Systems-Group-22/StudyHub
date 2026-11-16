"""Full-document retrieval boundary. Search wire contracts are deliberately absent."""
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.services.errors import (
    DocumentNotFound, InvalidSourceData, ProviderNotConfigured,
    SourceVersionExpired,
)


@dataclass(frozen=True)
class Chunk:
    document_id: str
    document_version: int
    chunk_id: str
    text: str
    page: int | None = None
    source_location: str | None = None


@dataclass
class DocumentBundle:
    chunks: list[Chunk]
    # Only a provider with full-document coverage may assert completeness.
    complete_versions: dict[str, int]


class DocumentProvider(Protocol):
    def get_documents(self, document_ids: list[str], document_versions: dict[str, int],
                      *, user_id: str) -> DocumentBundle: ...


class MockDocumentProvider:
    def get_documents(self, document_ids, document_versions, *, user_id):
        from app.services.mock_documents import load_mock_documents

        fixtures = load_mock_documents()
        chunks = []
        for doc_id in document_ids:
            doc = fixtures.get(doc_id)
            if doc is None:
                raise DocumentNotFound(f"No mock fixture for {doc_id}")
            if doc["version"] != document_versions[doc_id]:
                raise SourceVersionExpired(f"Mock fixture version differs for {doc_id}")
            chunks.append(Chunk(doc_id, doc["version"], f"{doc_id}:1",
                                doc["content"], source_location="mock-fixture"))
        return DocumentBundle(chunks, dict(document_versions))


class SearchDocumentProvider:
    """Own the authenticated client here; add the adapter once Search has a contract."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def get_documents(self, document_ids, document_versions, *, user_id):
        # TODO(Search): agree endpoint, user delegation, version-pinned full coverage,
        # pagination and mapping of not-processed/not-indexed/403/404/5xx errors.
        raise ProviderNotConfigured("Search full-document contract is not available")


def validate_bundle(bundle: DocumentBundle, versions: dict[str, int]) -> list[Chunk]:
    if bundle.complete_versions != versions or not bundle.chunks:
        raise InvalidSourceData("Provider did not confirm complete version-pinned documents")
    seen = set()
    documents = set()
    for chunk in bundle.chunks:
        if (chunk.document_id not in versions
                or type(chunk.document_version) is not int
                or chunk.document_version != versions[chunk.document_id]
                or not isinstance(chunk.chunk_id, str) or not chunk.chunk_id.strip()
                or not isinstance(chunk.text, str) or not chunk.text.strip()
                or (chunk.page is not None and (type(chunk.page) is not int or chunk.page < 1))
                or (chunk.source_location is not None and not isinstance(chunk.source_location, str))):
            raise InvalidSourceData("Invalid chunk identity, version, text or location")
        identity = (chunk.document_id, chunk.chunk_id)
        if identity in seen:
            raise InvalidSourceData("Duplicate chunk identity")
        seen.add(identity)
        documents.add(chunk.document_id)
    if documents != set(versions):
        raise InvalidSourceData("One or more full documents are missing")
    # Canonical document order matches the cache key; retain provider's chunk order.
    return sorted(bundle.chunks, key=lambda chunk: chunk.document_id)


def get_document_chunks(document_ids, document_versions, *, user_id):
    mode = os.getenv("DOCUMENT_PROVIDER", "search")
    if mode == "mock":
        if os.getenv("STUDYHUB_DEV_IDENTITY") != "1":
            raise ProviderNotConfigured("Mock documents require explicit development mode")
        provider = MockDocumentProvider()
        bundle = provider.get_documents(document_ids, document_versions, user_id=user_id)
    elif mode == "search":
        url = os.getenv("SEARCH_SERVICE_URL", "")
        token = os.getenv("STUDYHUB_SEARCH_TOKEN", "")
        if not url or not token:
            raise ProviderNotConfigured("Search URL and service token are required")
        with httpx.Client(base_url=url, timeout=15,
                          headers={"Authorization": f"Bearer {token}"}) as client:
            bundle = SearchDocumentProvider(client).get_documents(
                document_ids, document_versions, user_id=user_id)
    else:
        raise ProviderNotConfigured(f"Unknown document provider: {mode}")
    return validate_bundle(bundle, document_versions)
