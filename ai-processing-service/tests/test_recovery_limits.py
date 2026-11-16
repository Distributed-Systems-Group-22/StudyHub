"""Regressions for outage recovery, persisted cooldowns and bounded document work."""
import json
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import httpx
from src.budget import TaskBudget, BudgetExceeded, current_budget
from src.clients.model_client import ModelClient
from src.config import Settings
from src.consumer import Handler, parse_event
from src.pipeline import Pipeline
from src.processors.embeddings import chunk_text
from src.retry import is_transient, retry_delay
from test_processing import Store


def settings(**changes):
    """Supply dummy settings without reading local credentials."""
    return replace(Settings('postgresql://test', 'amqp://test', 'http://content', 'token',
        'http://models/api/chat', 'qwen', '', 'http://models/api/embed', 'bge', '',
        'http://speech', 'whisper', ''), **changes)


def http_error(code, retry_after=None):
    response = httpx.Response(code, headers={'Retry-After': retry_after} if retry_after else {},
                              request=httpx.Request('POST', 'http://models'))
    return httpx.HTTPStatusError('provider error', request=response.request, response=response)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.event = {'eventId': str(uuid4()), 'documentId': str(uuid4()),
                      'documentVersion': 1, 'eventType': 'document.uploaded'}
        self.store, self.pipeline, self.publish = Store(), Mock(), Mock()
        self.pipeline.process.return_value = {'summary': 'Recovered'}
        self.handler = Handler(settings(), self.store, self.pipeline, self.publish)

    def test_outage_recovers_after_more_than_max_attempts(self):
        for _ in range(5):
            self.pipeline.process.side_effect = httpx.ConnectError('offline')
            self.assertFalse(self.handler.handle(self.event))
            self.assertEqual(self.store.row['attempts'], 0)
            self.assertEqual(self.store.row['state'], 'pending')
            self.store.row['next_attempt_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.pipeline.process.side_effect = None
        self.assertTrue(self.handler.handle(self.event))
        self.pipeline.content.report.assert_called_once_with(self.event['documentId'], 1, 'succeeded')
        self.publish.assert_called_once()

    def test_cooldown_survives_handler_restart(self):
        self.pipeline.process.side_effect = http_error(429, '120')
        self.assertFalse(self.handler.handle(self.event))
        self.assertFalse(Handler(settings(), self.store, self.pipeline, self.publish).handle(self.event))
        self.pipeline.process.assert_called_once()
        self.pipeline.content.report.assert_not_called()
        self.assertGreater((self.store.row['next_attempt_at'] - datetime.now(timezone.utc)).total_seconds(), 119)

    def test_permanent_error_exhausts_budget(self):
        self.pipeline.process.side_effect = ValueError('unsupported format')
        self.assertFalse(self.handler.handle(self.event))
        self.assertFalse(self.handler.handle(self.event))
        self.assertTrue(self.handler.handle(self.event))
        self.pipeline.content.report.assert_called_once_with(self.event['documentId'], 1, 'failed')

    def test_storage_failure_is_not_a_model_failure(self):
        self.store.save = Mock(side_effect=RuntimeError('database unavailable'))
        with self.assertRaises(RuntimeError):
            self.handler.handle(self.event)
        self.pipeline.content.report.assert_not_called()

    def test_integer_boundaries(self):
        for version in (1, 2147483647):
            self.assertEqual(parse_event(json.dumps({**self.event, 'documentVersion': version}))['documentVersion'], version)
        for version in (-1, 0, True, 2147483648, 10 ** 50):
            with self.assertRaises(ValueError):
                parse_event(json.dumps({**self.event, 'documentVersion': version}))

    def test_transient_classification(self):
        for error in (httpx.ReadTimeout('timeout'), http_error(429), http_error(503), http_error(408)):
            self.assertTrue(is_transient(error))
        for error in (ValueError('bad file'), http_error(400), http_error(422)):
            self.assertFalse(is_transient(error))

    def test_backoff_and_retry_after(self):
        self.assertEqual([retry_delay(n, httpx.ConnectError('offline')) for n in range(7)], [10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(retry_delay(0, http_error(429, '120')), 120)
        date = (datetime.now(timezone.utc) + timedelta(seconds=100)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        self.assertGreater(retry_delay(0, http_error(503, date)), 98)
        self.assertEqual(retry_delay(0, http_error(503, 'invalid')), 10)


class LimitTests(unittest.TestCase):
    def test_pathological_chunking_rejected(self):
        for size, overlap in ((2000, 1999), (1, 0)):
            with self.assertRaises(ValueError):
                chunk_text('x' * 200000, size, overlap)

    def test_chunk_count_matches_slices(self):
        for length in range(1, 50):
            chunks = chunk_text('x' * length, 8, 2, max_chunks=100)
            self.assertEqual(chunks[-1]['endOffset'], length)
            with self.assertRaises(ValueError):
                chunk_text('x' * length, 8, 2, max_chunks=len(chunks) - 1)

    def test_chunk_limit_checked_before_model_work(self):
        s = settings(chunk_size=1, chunk_overlap=0)
        content, models = Mock(), Mock()
        models.settings = s
        content.download.return_value = (b'x' * 1000, 'test.txt')
        with self.assertRaises(ValueError):
            Pipeline(s, content, models).process(str(uuid4()), 1)
        models.summarize.assert_not_called()
        models.embed.assert_not_called()

    def test_shared_call_limit(self):
        s = settings(max_model_calls=1)
        content, models = Mock(), Mock()
        models.settings = s
        models.summarize.return_value = 'Summary'
        content.download.return_value = (b'Study material', 'test.txt')
        with self.assertRaises(BudgetExceeded):
            Pipeline(s, content, models).process(str(uuid4()), 1)
        models.summarize.assert_called_once()
        models.embed.assert_not_called()
        self.assertEqual(models.release.call_count, 2)

    def test_deadline_prevents_calls(self):
        with patch('src.budget.time.monotonic', return_value=0):
            budget = TaskBudget(10, 5)
        function = Mock()
        with patch('src.budget.time.monotonic', return_value=6):
            with self.assertRaises(BudgetExceeded):
                budget.call(function)
        function.assert_not_called()
        self.assertIsNone(current_budget.get())

    def test_http_timeout_uses_remaining_budget(self):
        client = ModelClient(settings(timeout=300))
        response, transport = Mock(), Mock()
        response.iter_bytes.return_value = [b'{"ok":true}']
        transport.stream.return_value.__enter__ = Mock(return_value=response)
        transport.stream.return_value.__exit__ = Mock(return_value=False)
        with patch('src.clients.model_client.httpx.Client') as factory:
            factory.return_value.__enter__.return_value = transport
            TaskBudget(1, 10).call(client._post, 'http://models', '')
            self.assertLessEqual(factory.call_args.kwargs['timeout'], 10)

    def test_invalid_resource_settings(self):
        for changes in ({'max_chunks': 0}, {'max_model_calls': 0}, {'max_job_seconds': float('inf')},
                        {'retry_base_seconds': 500}, {'timeout': float('nan')}):
            with self.assertRaises(ValueError):
                settings(**changes).validate()
