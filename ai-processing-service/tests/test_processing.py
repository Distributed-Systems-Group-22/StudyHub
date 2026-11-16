"""Delivery and pipeline regressions using an in-memory job store."""
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from src.consumer import Handler, parse_event
from src.clients.content_client import DocumentUnavailable
from src.clients.model_client import ModelClient
from src.pipeline import Pipeline
from src.processors.embeddings import chunk_text
from src.processors.extraction import extract_text


class Store:
    """Mirror persisted attempt and cooldown transitions without external services."""
    def __init__(self):
        self.row = {'state': 'pending', 'attempts': 0, 'result_event_id': uuid4()}

    @contextmanager
    def lock(self, event):
        yield self

    def read(self):
        return self.row.copy()

    def attempt(self):
        self.row['attempts'] += 1

    def defer(self, delay, error_type):
        self.row['attempts'] = max(0, self.row['attempts'] - 1)
        self.row['retry_count'] = self.row.get('retry_count', 0) + 1
        self.row['next_attempt_at'] = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.row['error_type'] = error_type

    def save(self, state, result=None, error_type=None):
        self.row.update(state=state, result=result, error_type=error_type, next_attempt_at=None)


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.event = {'eventId': str(uuid4()), 'eventType': 'document.uploaded',
                      'documentId': str(uuid4()), 'documentVersion': 1}
        self.settings = SimpleNamespace(max_attempts=2, chunk_size=8, chunk_overlap=2,
                                        embedding_model='test', summary_model='test',
                                        model_provider='ollama', ollama_context=4096, summary_max_tokens=512)
        self.store = Store()
        self.pipeline = Mock()
        self.pipeline.process.return_value = {'summary': 'example'}
        self.publisher = Mock()
        self.handler = Handler(self.settings, self.store, self.pipeline, self.publisher)

    def test_duplicate_does_not_repeat_models_or_publication(self):
        self.assertTrue(self.handler.handle(self.event))
        self.assertTrue(self.handler.handle(self.event))
        self.pipeline.process.assert_called_once()
        self.publisher.assert_called_once()

    def test_publish_failure_keeps_result_for_retry(self):
        self.publisher.side_effect = [RuntimeError(), None]
        with self.assertRaises(RuntimeError):
            self.handler.handle(self.event)
        event_id = self.store.row['result_event_id']
        self.assertEqual(self.store.row['state'], 'result_ready')
        self.assertTrue(self.handler.handle(self.event))
        self.pipeline.process.assert_called_once()
        self.assertEqual(self.store.row['result_event_id'], event_id)

    def test_callback_failure_does_not_repeat_models(self):
        self.pipeline.content.report.side_effect = [RuntimeError(), None]
        with self.assertRaises(RuntimeError):
            self.handler.handle(self.event)
        self.assertTrue(self.handler.handle(self.event))
        self.pipeline.process.assert_called_once()

    def test_bounded_failure_reports_failed_without_output(self):
        self.pipeline.process.side_effect = ValueError('unsupported')
        self.assertFalse(self.handler.handle(self.event))
        self.assertTrue(self.handler.handle(self.event))
        self.pipeline.content.report.assert_called_once_with(
            self.event['documentId'], 1, 'failed')
        self.publisher.assert_not_called()

    def test_deleted_during_callback_discards_result(self):
        self.pipeline.content.report.side_effect = DocumentUnavailable()
        self.assertTrue(self.handler.handle(self.event))
        self.assertEqual(self.store.row['state'], 'skipped')
        self.assertIsNone(self.store.row['result'])
        self.publisher.assert_not_called()

    def test_crash_exhaustion_does_not_call_models_again(self):
        self.store.row['attempts'] = 2
        self.assertTrue(self.handler.handle(self.event))
        self.pipeline.process.assert_not_called()

    def test_chunk_offsets_and_overlap(self):
        text = 'abcdefghijklmnop'
        chunks = chunk_text(text, 8, 2)
        self.assertEqual([c['text'] for c in chunks], ['abcdefgh', 'ghijklmn', 'mnop'])
        for chunk in chunks:
            self.assertEqual(chunk['text'], text[chunk['startOffset']:chunk['endOffset']])

    def test_invalid_event_version(self):
        import json
        for version in (True, 0, '1', 2147483648):
            with self.assertRaises(ValueError):
                parse_event(json.dumps({**self.event, 'documentVersion': version}))

    def test_malformed_ids_are_rejected(self):
        import json
        for value in (None, 123, [], {}):
            with self.assertRaises(ValueError):
                parse_event(json.dumps({**self.event, 'documentId': value}))

    def test_empty_or_unsupported_input(self):
        for content, filename in [(b'', 'x.txt'), (b'abc', 'x.exe')]:
            with self.assertRaises(ValueError):
                extract_text(content, filename)

    def test_pipeline_with_fake_models(self):
        content = Mock()
        content.download.return_value = (b'Hello study material', 'notes.txt')
        models = Mock()
        models.settings = self.settings
        models.summarize.return_value = 'Summary'
        models.embed.return_value = [0.1, 0.2]
        result = Pipeline(self.settings, content, models).process(self.event['documentId'], 1)
        self.assertEqual(result['summary'], 'Summary')
        self.assertTrue(all(c['embedding'] == [0.1, 0.2] for c in result['chunks']))

    def test_missing_endpoint_rejected_without_network(self):
        with self.assertRaises(ValueError):
            ModelClient(self.settings)._post('', '')


if __name__ == '__main__':
    unittest.main()
