import os
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.services import ai_client
from app.services.cache import generate_versioned_cache_key
from app.services.document_provider import Chunk

JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
TOKEN = UUID("22222222-2222-4222-8222-222222222222")


class SynthesisTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "DOCUMENT_PROVIDER": "mock", "STUDYHUB_DEV_IDENTITY": "1",
            "MOCK_DOCUMENTS_FILE": "", "SEARCH_SERVICE_URL": "",
            "AI_CONTEXT_SIZE": "2048", "AI_OUTPUT_TOKENS": "96",
            "AI_TEMPERATURE": "0.2", "AI_SEED": "42", "AI_MAX_CALLS": "64",
            "AI_MAX_SOURCE_BYTES": "2000000", "RETRIEVAL_VERSION": "unit-test-v1",
            "RABBITMQ_USER": "test", "RABBITMQ_PASSWORD": "test",
            "STUDYHUB_SYNTHESIS_TOKEN": "unit-test-token",
        }))
        self.enterContext(patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Unit test attempted real HTTP access")))
        self.enterContext(patch("pika.BlockingConnection", side_effect=AssertionError("Unit test attempted real broker access")))
        self.enterContext(patch("app.database.psycopg.connect", side_effect=AssertionError("Unit test attempted real database access")))
        self.enterContext(patch.object(ai_client, "resolve_model_revision", return_value="digest-v1"))
        self.config = ai_client.get_generation_config()
        self.chunks = [Chunk("doc-601", 1, "c1", "Outboxes persist events.", page=1),
                       Chunk("doc-602", 1, "c2", "Consumers handle duplicates.", source_location="section-2")]
        self.versions = {chunk.document_id: chunk.document_version for chunk in self.chunks}
        self.job = {
            "job_id": JOB_ID, "user_id": "alice", "document_ids": list(self.versions),
            "instruction": "Summarise", "document_versions": self.versions,
            "generation_config": self.config, "claim_token": TOKEN,
            "status": "RUNNING", "attempts": 1,
            "lease_until": datetime.now(timezone.utc) + timedelta(seconds=45),
            "next_attempt_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        self.job["cache_key"] = generate_versioned_cache_key(
            "alice", [{"documentId": d, "documentVersion": v} for d, v in self.versions.items()],
            "Summarise", self.config)

    def database(self, module, *, rows=(), fetches=()):
        connection, cursor = MagicMock(), MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.rowcount = 1
        cursor.fetchall.return_value = list(rows)
        cursor.fetchone.side_effect = list(fetches)
        self.enterContext(patch.object(module, "get_connection", return_value=connection))
        return connection, cursor


def statements(cursor):
    return [" ".join(call.args[0].split()) for call in cursor.execute.call_args_list]
