from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import patch

from app.services import job_execution as jobs
from app.services.errors import GenerationConfigChanged, InvalidCitation, InvalidSourceData, ProviderUnavailable, SourceVersionExpired, BudgetExceeded
from tests.support import JOB_ID, TOKEN, SynthesisTestCase, statements


class JobStorageTests(SynthesisTestCase):
    def test_only_pending_jobs_can_be_claimed(self):
        for row in (None, self.job, {**self.job, "status": "COMPLETED"}):
            with self.subTest(row=row):
                _, cursor = self.database(jobs, fetches=[row])
                self.assertIsNone(jobs.claim_job(JOB_ID))
                self.assertEqual(len(statements(cursor)), 1)

    def test_cooldown_prevents_early_claim(self):
        self.database(jobs, fetches=[{**self.job, "status": "PENDING", "next_attempt_at": datetime.now(timezone.utc) + timedelta(seconds=30)}])
        self.assertIsNone(jobs.claim_job(JOB_ID))

    def test_claim_assigns_token_and_increments_attempt(self):
        _, cursor = self.database(jobs, fetches=[{**self.job, "status": "PENDING"}])
        result = jobs.claim_job(JOB_ID)
        self.assertEqual(result["attempt"], 2)
        self.assertNotEqual(result["claim_token"], TOKEN)
        self.assertIn("status = 'RUNNING'", statements(cursor)[-1])

    def test_exhausted_or_unsafe_jobs_fail_before_execution(self):
        for changes in ({"attempts": jobs.MAX_ATTEMPTS}, {"generation_config": None}, {"document_versions": None}):
            with self.subTest(changes=changes):
                _, cursor = self.database(jobs, fetches=[{**self.job, "status": "PENDING", **changes}])
                self.assertIsNone(jobs.claim_job(JOB_ID))
                self.assertIn("status = 'FAILED'", statements(cursor)[-1])

    def test_renewal_reports_lost_ownership(self):
        self.database(jobs, fetches=[None])
        self.assertFalse(jobs.renew_lease(self.job))

    def test_completion_records_all_inputs_including_uncited_sources(self):
        connection, cursor = self.database(jobs, fetches=[None, (JOB_ID,)])
        self.assertTrue(jobs.complete_job(self.job, "summary", "digest-v1", self.config["prompt_version"], self.chunks, []))
        inserts = [call for call in cursor.execute.call_args_list if "INSERT INTO summary_sources" in call.args[0]]
        self.assertEqual([call.args[1][1] for call in inserts], list(self.versions))
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_deleted_source_cannot_be_committed(self):
        connection, cursor = self.database(jobs, fetches=[("deleted",)])
        with self.assertRaises(PermissionError):
            jobs.complete_job(self.job, "summary", "digest-v1", self.config["prompt_version"], self.chunks, [])
        self.assertFalse(any("INSERT INTO synthesis_results" in s for s in statements(cursor)))
        self.assertIs(connection.__exit__.call_args.args[0], PermissionError)

    def test_stale_claim_cannot_insert_result(self):
        _, cursor = self.database(jobs, fetches=[None, None])
        self.assertFalse(jobs.complete_job(self.job, "summary", "digest-v1", self.config["prompt_version"], self.chunks, []))
        self.assertFalse(any("INSERT INTO" in s for s in statements(cursor)))

    def test_wrong_result_model_fails_before_database(self):
        with patch.object(jobs, "get_connection") as database, self.assertRaises(InvalidSourceData):
            jobs.complete_job(self.job, "summary", "wrong-model", self.config["prompt_version"], self.chunks, [])
        database.assert_not_called()

    def test_transient_failure_schedules_retry_and_outbox_in_same_transaction(self):
        connection, cursor = self.database(jobs, fetches=[self.job])
        before = datetime.now(timezone.utc)
        jobs.fail_job(self.job, ProviderUnavailable("offline"))
        queries = statements(cursor)
        self.assertIn("status = 'PENDING'", queries[1])
        self.assertIn("INSERT INTO outbox_events", queries[2])
        retry_at = cursor.execute.call_args_list[1].args[1][0]
        self.assertGreaterEqual(retry_at, before + timedelta(seconds=2))
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_permanent_or_exhausted_failure_does_not_schedule_retry(self):
        for error, attempts in [(InvalidCitation("invalid"), 1), (ProviderUnavailable("offline"), 3)]:
            with self.subTest(error=error, attempts=attempts):
                _, cursor = self.database(jobs, fetches=[{**self.job, "attempts": attempts}])
                jobs.fail_job(self.job, error)
                self.assertIn("status = 'FAILED'", statements(cursor)[-1])
                self.assertFalse(any("outbox_events" in s for s in statements(cursor)))

    def test_unknown_exception_does_not_expose_its_message(self):
        _, cursor = self.database(jobs, fetches=[self.job])
        jobs.fail_job(self.job, RuntimeError("private document contents"))
        self.assertEqual(cursor.execute.call_args_list[1].args[1][1], "infrastructure_error: RuntimeError")

    def test_failure_from_expired_or_replaced_claim_does_not_change_job(self):
        for changes in ({"claim_token": "another-worker"}, {"status": "COMPLETED"},
                        {"lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)}):
            with self.subTest(changes=changes):
                _, cursor = self.database(jobs, fetches=[{**self.job, **changes}])
                jobs.fail_job(self.job, ProviderUnavailable("offline"))
                self.assertEqual(len(statements(cursor)), 1)


class JobPipelineTests(SynthesisTestCase):
    def setUp(self):
        super().setUp()
        self.verify_snapshot = jobs.verify_source_snapshot
        self.claim = self.enterContext(patch.object(jobs, "claim_job", return_value=self.job))
        self.access = self.enterContext(patch.object(jobs, "verify_source_snapshot", return_value=self.versions))
        self.retrieve = self.enterContext(patch.object(jobs, "get_document_chunks", return_value=self.chunks))
        self.generate = self.enterContext(patch.object(jobs, "generate_summary", return_value={
            "summary_text": "original", "model_version": "digest-v1", "prompt_version": self.config["prompt_version"], "citations": []}))
        self.complete = self.enterContext(patch.object(jobs, "complete_job", return_value=True))
        self.fail_job = self.enterContext(patch.object(jobs, "fail_job"))

    def test_complete_pipeline_rechecks_sources_and_saves_all_chunks(self):
        jobs.process_job(JOB_ID)
        self.assertEqual(self.access.call_count, 2)
        self.assertEqual(self.complete.call_args.args[-2], self.chunks)
        self.fail_job.assert_not_called()

    def test_duplicate_or_unclaimable_delivery_does_no_work(self):
        self.claim.return_value = None
        jobs.process_job(JOB_ID)
        self.generate.assert_not_called()

    def test_queued_version_change_stops_before_retrieval(self):
        self.access.return_value = {**self.versions, "doc-601": 2}
        jobs.process_job(JOB_ID)
        self.retrieve.assert_not_called()
        self.assertIsInstance(self.fail_job.call_args.args[1], SourceVersionExpired)

    def test_version_change_during_generation_stops_commit(self):
        self.access.side_effect = [self.versions, {**self.versions, "doc-601": 2}]
        jobs.process_job(JOB_ID)
        self.complete.assert_not_called()
        self.assertIsInstance(self.fail_job.call_args.args[1], SourceVersionExpired)

    def test_configuration_change_stops_before_model_work(self):
        self.job["generation_config"] = {**self.config, "model_revision": "old-model"}
        jobs.process_job(JOB_ID)
        self.generate.assert_not_called()
        self.assertIsInstance(self.fail_job.call_args.args[1], GenerationConfigChanged)

    def test_tampered_cache_snapshot_is_rejected(self):
        self.job["cache_key"] = "tampered"
        jobs.process_job(JOB_ID)
        self.generate.assert_not_called()
        self.assertIsInstance(self.fail_job.call_args.args[1], InvalidSourceData)

    def test_model_failure_is_routed_to_retry_policy(self):
        self.generate.side_effect = ProviderUnavailable("offline")
        jobs.process_job(JOB_ID)
        self.complete.assert_not_called()
        self.assertIsInstance(self.fail_job.call_args.args[1], ProviderUnavailable)

    def test_cancelled_delivery_does_not_fail_or_commit_owned_job(self):
        cancelled = threading.Event()
        cancelled.set()
        jobs.process_job(JOB_ID, cancelled)
        self.generate.assert_not_called()
        self.fail_job.assert_not_called()
        self.complete.assert_not_called()

    def test_deadline_checkpoint_stops_generation(self):
        with patch.object(jobs.time, "monotonic", side_effect=[0, jobs.MAX_JOB_SECONDS + 1]):
            with self.assertRaises(BudgetExceeded):
                with jobs.job_lease(self.job, threading.Event()):
                    self.fail("Expired lease should not yield")

    def test_missing_permission_snapshot_is_not_complete(self):
        with patch.object(jobs, "verify_document_access", return_value=[]), self.assertRaises(PermissionError):
            self.verify_snapshot("alice", list(self.versions))
