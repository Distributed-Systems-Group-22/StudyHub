
# Synthetic documents for local integration testing.
# No real user content is used.
import json
import os
from pathlib import Path

from app.services.errors import ProviderNotConfigured

MOCK_DOCUMENTS = {
    "doc-601": {
        "title": "Microservices Architecture",
        "version": 1,
        "content": """
Microservices divide an application into independently
deployable services.

Each service is responsible for a specific business
capability and owns its data.

Services communicate through synchronous APIs or
asynchronous messages.

Benefits include independent deployment, fault
isolation and scalability.

Challenges include network failures, distributed
transactions and data consistency.
"""
    },

    "doc-602": {
        "title": "Message Queues and Resilience",
        "version": 1,
        "content": """
Message queues support asynchronous communication
between distributed services.

RabbitMQ allows producers to publish messages while
consumers process them independently.

Acknowledgements help avoid message loss when a
consumer crashes.

Idempotent consumers prevent duplicate messages
from creating duplicate effects.

Retry with backoff reduces pressure during temporary
failures.

Transactional outbox prevents database commits from
being disconnected from message publication.

Circuit breakers help prevent repeated calls to
unavailable downstream services.
"""
    }
}


def load_mock_documents():
    """Optional version-pinned fixtures can match actual Content development UUIDs."""
    path = os.getenv("MOCK_DOCUMENTS_FILE")
    if not path:
        return MOCK_DOCUMENTS
    try:
        fixture = Path(path)
        if fixture.stat().st_size > 2_000_000:
            raise ValueError("Mock fixture exceeds 2 MB")
        documents = json.loads(fixture.read_text(encoding="utf-8-sig"))
        if not isinstance(documents, dict) or not documents:
            raise ValueError("Expected a nonempty document map")
        for doc_id, doc in documents.items():
            if (not isinstance(doc_id, str) or not doc_id.strip() or not isinstance(doc, dict)
                    or type(doc.get("version")) is not int or doc["version"] < 1
                    or not isinstance(doc.get("content"), str) or not doc["content"].strip()):
                raise ValueError("Invalid mock document fixture")
        return documents
    except (OSError, ValueError) as error:
        raise ProviderNotConfigured("Invalid MOCK_DOCUMENTS_FILE") from error


def get_documents(document_ids):
    fixtures = load_mock_documents()
    documents = []

    for document_id in document_ids:
        if document_id not in fixtures:
            raise ValueError(
                f"Mock document not found: {document_id}"
            )

        documents.append({
            "document_id": document_id,
            **fixtures[document_id]
        })

    return documents
