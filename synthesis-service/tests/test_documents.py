from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx

from app.services import ai_client as ai, content_client as content
from app.services.citations import source_catalog, validate_citations
from app.services.document_provider import DocumentBundle, get_document_chunks, validate_bundle
from app.services.errors import DocumentNotFound, GenerationConfigChanged, InvalidCitation, InvalidSourceData, ProviderNotConfigured, SourceVersionExpired
from tests.support import SynthesisTestCase


class DocumentTests(SynthesisTestCase):
    def test_complete_bundle_is_canonical_but_keeps_chunk_order(self):
        chunks = validate_bundle(DocumentBundle(list(reversed(self.chunks)), self.versions), self.versions)
        self.assertEqual(chunks, self.chunks)

    def test_missing_duplicate_or_stale_chunks_are_rejected(self):
        cases = [self.chunks[:1], self.chunks + self.chunks[:1],
                 [replace(self.chunks[0], document_version=2), self.chunks[1]]]
        for chunks in cases:
            with self.subTest(chunks=chunks), self.assertRaises(InvalidSourceData):
                validate_bundle(DocumentBundle(chunks, self.versions), self.versions)

    def test_invalid_chunk_fields_are_rejected(self):
        for changes in ({"text": " "}, {"chunk_id": ""}, {"page": True}, {"page": 0},
                        {"source_location": 3}, {"document_version": True}):
            with self.subTest(changes=changes), self.assertRaises(InvalidSourceData):
                validate_bundle(DocumentBundle([replace(self.chunks[0], **changes), self.chunks[1]], self.versions), self.versions)

    def test_provider_must_confirm_complete_versions(self):
        with self.assertRaises(InvalidSourceData):
            validate_bundle(DocumentBundle(self.chunks, {"doc-601": 1}), self.versions)

    def test_mock_provider_returns_matching_sources(self):
        result = get_document_chunks(list(reversed(self.versions)), self.versions, user_id="alice")
        self.assertEqual([c.document_id for c in result], list(self.versions))

    def test_mock_requires_explicit_development_mode(self):
        with patch.dict(os.environ, {"STUDYHUB_DEV_IDENTITY": "0"}), self.assertRaises(ProviderNotConfigured):
            get_document_chunks(list(self.versions), self.versions, user_id="alice")

    def test_missing_and_stale_mock_documents_fail(self):
        for versions, error in [({"missing": 1}, DocumentNotFound), ({"doc-601": 2}, SourceVersionExpired)]:
            with self.subTest(versions=versions), self.assertRaises(error):
                get_document_chunks(list(versions), versions, user_id="alice")

    def test_search_fails_closed_even_when_configured(self):
        for url in ("", "http://search.invalid"):
            with self.subTest(url=url), patch.dict(os.environ, {"DOCUMENT_PROVIDER": "search", "SEARCH_SERVICE_URL": url, "STUDYHUB_SEARCH_TOKEN": "test"}), self.assertRaises(ProviderNotConfigured):
                get_document_chunks(list(self.versions), self.versions, user_id="alice")

    def test_fixture_changes_invalidate_generation_snapshot(self):
        with tempfile.TemporaryDirectory() as scratch:
            file = Path(scratch) / "documents.json"
            file.write_text(json.dumps({"doc-601": {"version": 1, "content": "Original"}}), encoding="utf-8")
            with patch.dict(os.environ, {"MOCK_DOCUMENTS_FILE": str(file)}):
                old = ai.get_generation_config()
                file.write_text(json.dumps({"doc-601": {"version": 1, "content": "Changed"}}), encoding="utf-8")
                with self.assertRaises(GenerationConfigChanged):
                    ai.ensure_generation_config(old)

    def test_invalid_fixture_is_not_treated_as_empty_sources(self):
        with tempfile.TemporaryDirectory() as scratch:
            file = Path(scratch) / "documents.json"
            for text in ("[]", "broken json", '{"a":{"version":true,"content":"text"}}'):
                file.write_text(text, encoding="utf-8")
                with self.subTest(text=text), patch.dict(os.environ, {"MOCK_DOCUMENTS_FILE": str(file)}), self.assertRaises(ProviderNotConfigured):
                    get_document_chunks(["a"], {"a": 1}, user_id="alice")

    def test_citations_preserve_metadata_and_deduplicate_labels(self):
        citations = validate_citations("One [S2], two [S1], again [S2].", source_catalog(self.chunks))
        self.assertEqual([c["document_id"] for c in citations], ["doc-602", "doc-601"])
        self.assertEqual(citations[0]["source_location"], "section-2")
        self.assertEqual(citations[1]["page"], 1)

    def test_unknown_missing_and_broken_citations_fail(self):
        for text in ("Missing", "Unknown [S99]", "Broken [S1", "Wrong [doc-601]"):
            with self.subTest(text=text), self.assertRaises(InvalidCitation):
                validate_citations(text, source_catalog(self.chunks))


class ContentClientTests(SynthesisTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(content, "SYNTHESIS_TOKEN", "test-token"))
        self.enterContext(patch.object(content, "CONTENT_SERVICE_URL", "http://content.invalid"))

    def respond(self, payload=None, status=200, text=None):
        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            self.assertEqual(request.headers["User_Name"], "alice")
            self.assertEqual(json.loads(request.content), {"documentIds": ["a"]})
            return httpx.Response(status, json=payload) if text is None else httpx.Response(status, text=text)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.enterContext(patch.object(content.httpx, "Client", return_value=client))

    def test_authorized_metadata_is_returned(self):
        documents = [{"documentId": "a", "documentVersion": 1}]
        self.respond({"documents": documents})
        self.assertEqual(content.verify_document_access("alice", ["a"]), documents)

    def test_missing_permission_returns_no_document(self):
        self.respond({"documents": []})
        self.assertEqual(content.verify_document_access("alice", ["a"]), [])

    def test_empty_request_does_not_contact_content(self):
        with patch.object(content.httpx, "Client") as client:
            self.assertEqual(content.verify_document_access("alice", []), [])
        client.assert_not_called()

    def test_missing_token_fails_before_network(self):
        with patch.object(content, "SYNTHESIS_TOKEN", ""), self.assertRaises(content.ContentServiceUnavailable):
            content.verify_document_access("alice", ["a"])

    def test_transport_failure_is_service_unavailable(self):
        with patch.object(content.httpx, "Client", side_effect=httpx.ConnectError("offline")), self.assertRaises(content.ContentServiceUnavailable):
            content.verify_document_access("alice", ["a"])

    def test_non_success_status_fails_closed(self):
        self.respond({}, status=503)
        with self.assertRaises(content.ContentServiceUnavailable):
            content.verify_document_access("alice", ["a"])

    def test_invalid_json_fails_closed(self):
        self.respond(text="not json")
        with self.assertRaises(content.ContentServiceUnavailable):
            content.verify_document_access("alice", ["a"])

    def test_malformed_or_duplicate_metadata_fails_closed(self):
        for payload in ([], {}, {"documents": [None]},
                        {"documents": [{"documentId": "a", "documentVersion": True}]},
                        {"documents": [{"documentId": "other", "documentVersion": 1}]},
                        {"documents": [{"documentId": "a", "documentVersion": 1}] * 2}):
            with self.subTest(payload=payload), patch.object(content.httpx, "Client") as factory:
                factory.return_value.__enter__.return_value.post.return_value = httpx.Response(200, json=payload)
                with self.assertRaises(content.ContentServiceUnavailable):
                    content.verify_document_access("alice", ["a"])
