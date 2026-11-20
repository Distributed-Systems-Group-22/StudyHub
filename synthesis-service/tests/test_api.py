import os
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg
from pydantic import ValidationError

from app.main import app
from app.routes import summaries, jobs
from app.schemas import SummaryRequest
from app.services.content_client import ContentServiceUnavailable
from tests.support import JOB_ID, SynthesisTestCase, statements


class ApiTests(SynthesisTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.access = self.enterContext(patch.object(summaries, "verify_source_snapshot", return_value=self.versions))
        self.enterContext(patch.object(summaries, "get_generation_config", return_value=self.config))
        self.cached = self.enterContext(patch.object(summaries, "find_cached_job", return_value=None))
        self.headers = {"User_Name": "alice"}

    def post(self, **kwargs):
        return self.client.post("/api/v1/summaries", headers=self.headers, json={"document_ids": list(self.versions)}, **kwargs)

    def test_whitespace_uuid_and_duplicate_validation(self):
        request = SummaryRequest(document_ids=["{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"], instruction=" Summarise ")
        self.assertEqual(request.document_ids, ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])
        self.assertEqual(request.instruction, "Summarise")
        with self.assertRaises(ValidationError):
            SummaryRequest(document_ids=["a", " a "])

    def test_invalid_input_is_http_422_before_dependencies(self):
        for body in ({"document_ids": []}, {"document_ids": [" "]},
                     {"document_ids": ["a"], "instruction": " "},
                     {"document_ids": [str(i) for i in range(21)]}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/v1/summaries", headers=self.headers, json=body).status_code, 422)
        self.access.assert_not_called()

    def test_development_identity_errors(self):
        for headers, expected in [({}, 401), ({"User_Name": "unknown"}, 403)]:
            self.assertEqual(self.client.post("/api/v1/summaries", headers=headers, json={"document_ids": ["a"]}).status_code, expected)
        with patch.dict(os.environ, {"STUDYHUB_DEV_IDENTITY": "0"}):
            self.assertEqual(self.post().status_code, 503)

    def test_access_denial_and_content_outage_are_distinct(self):
        for error, expected in [(PermissionError(), 403), (ContentServiceUnavailable(), 503)]:
            self.access.side_effect = error
            self.assertEqual(self.post().status_code, expected)

    def test_cached_job_is_reused_without_new_outbox(self):
        self.cached.return_value = JOB_ID
        _, cursor = self.database(summaries)
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["cache_hit"])
        cursor.execute.assert_not_called()

    def test_active_job_is_reused_without_new_outbox(self):
        _, cursor = self.database(summaries, fetches=[{"job_id": JOB_ID, "status": "RUNNING"}])
        response = self.post()
        self.assertEqual(response.json()["status"], "RUNNING")
        self.assertFalse(response.json()["cache_hit"])
        self.assertFalse(any("INSERT INTO" in s for s in statements(cursor)))

    def test_new_job_and_outbox_share_a_transaction(self):
        connection, cursor = self.database(summaries, fetches=[None, {"job_id": JOB_ID}])
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "PENDING")
        self.assertTrue(any("INSERT INTO synthesis_jobs" in s for s in statements(cursor)))
        payload = cursor.execute.call_args.args[1][-1].obj
        self.assertEqual(payload["document_ids"], list(self.versions))
        self.assertEqual(payload["user_id"], "alice")
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_insert_race_reuses_winning_job(self):
        _, cursor = self.database(summaries, fetches=[None, None, {"job_id": JOB_ID, "status": "PENDING"}])
        response = self.post()
        self.assertEqual(response.json()["job_id"], str(JOB_ID))
        self.assertFalse(any("INSERT INTO outbox_events" in s for s in statements(cursor)))

    def test_database_error_returns_503_and_rolls_back(self):
        connection, cursor = self.database(summaries)
        cursor.execute.side_effect = psycopg.OperationalError("offline")
        self.assertEqual(self.post().status_code, 503)
        self.assertIs(connection.__exit__.call_args.args[0], psycopg.OperationalError)


class JobApiTests(SynthesisTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.access = self.enterContext(patch.object(jobs, "verify_document_access", return_value=[
            {"documentId": d, "documentVersion": v} for d, v in self.versions.items()]))
        self.sources = [{"document_id": d, "document_version": v} for d, v in self.versions.items()]
        self.result = {"job_id": JOB_ID, "status": "COMPLETED", "attempts": 1, "summary_id": JOB_ID,
                       "summary_text": "private summary", "summary_status": "VALID", "citations": []}

    def get(self):
        return self.client.get(f"/api/v1/summaries/jobs/{JOB_ID}", headers={"User_Name": "alice"})

    def test_missing_or_unowned_job_is_404(self):
        self.database(jobs, fetches=[None])
        self.assertEqual(self.get().status_code, 404)

    def test_incomplete_job_never_exposes_text_or_citations(self):
        self.database(jobs, fetches=[{**self.result, "status": "RUNNING"}])
        body = self.get().json()
        self.assertIsNone(body["summary_text"])
        self.assertIsNone(body["citations"])
        self.access.assert_not_called()

    def test_invalidated_or_missing_result_is_409(self):
        for changes in ({"summary_status": "INVALIDATED"}, {"summary_id": None}):
            with self.subTest(changes=changes):
                self.database(jobs, fetches=[{**self.result, **changes}])
                self.assertEqual(self.get().status_code, 409)

    def test_completed_result_requires_source_metadata(self):
        self.database(jobs, fetches=[self.result])
        self.assertEqual(self.get().status_code, 409)

    def test_completed_result_requires_current_access_and_version(self):
        self.database(jobs, rows=self.sources, fetches=[self.result])
        self.access.return_value = []
        self.assertEqual(self.get().status_code, 404)

    def test_content_outage_does_not_expose_cached_summary(self):
        self.database(jobs, rows=self.sources, fetches=[self.result])
        self.access.side_effect = ContentServiceUnavailable("offline")
        response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private summary", response.text)

    def test_invalidation_during_access_check_blocks_result(self):
        self.database(jobs, rows=self.sources, fetches=[self.result, {"summary_status": "INVALIDATED", "summary_text": "private"}])
        self.assertEqual(self.get().status_code, 409)

    def test_current_result_is_served_after_recheck(self):
        self.database(jobs, rows=self.sources, fetches=[self.result, {"summary_status": "VALID", "summary_text": "verified"}])
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary_text"], "verified")
