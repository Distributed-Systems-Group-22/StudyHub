"""Provider wire-format tests with fake model responses."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from src.clients.model_client import ModelClient
from src.processors.summarization import summarize_document


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(model_provider='ollama', summary_url='http://localhost/api/chat',
            summary_model='qwen3:4b', summary_key='', embedding_url='http://localhost/api/embed',
            embedding_model='bge-m3', embedding_key='', ollama_context=4096, summary_max_tokens=512,
            transcription_url='http://localhost:8001/v1/audio/transcriptions',
            transcription_model='mlx-community/whisper-small-mlx', transcription_key='')
        self.client = ModelClient(self.settings)
        self.client._post = Mock()

    def test_native_summary_disables_thinking(self):
        self.client._post.return_value = {'message': {'content': 'Summary'}, 'done_reason': 'stop'}
        self.assertEqual(self.client.summarize('Some text'), 'Summary')
        payload = self.client._post.call_args.kwargs['json']
        self.assertFalse(payload['think'])
        self.assertFalse(payload['stream'])
        self.assertEqual(payload['options']['num_ctx'], 4096)

    def test_qwen_thinking_is_not_stored_as_summary(self):
        self.client._post.return_value = {'message': {'content': 'thinking</think>Final summary'}}
        self.assertEqual(self.client.summarize('Text'), 'Final summary')
        self.assertTrue(self.client._post.call_args.kwargs['json']['messages'][1]['content'].endswith('/no_think'))

    def test_native_embedding_no_truncation(self):
        self.client._post.return_value = {'embeddings': [[0.1, 0.2]]}
        self.assertEqual(self.client.embed('Text'), [0.1, 0.2])
        self.assertFalse(self.client._post.call_args.kwargs['json']['truncate'])

    def test_third_party_format_retained(self):
        self.settings.model_provider = 'openai'
        self.client._post.return_value = {'choices': [{'message': {'content': 'Summary'}}]}
        self.assertEqual(self.client.summarize('Text'), 'Summary')
        self.assertNotIn('think', self.client._post.call_args.kwargs['json'])
        self.client._post.return_value = {'data': [{'embedding': [0.2]}]}
        self.assertEqual(self.client.embed('Text'), [0.2])

    def test_reject_nonfinite_embedding(self):
        self.client._post.return_value = {'embeddings': [[float('nan')]]}
        with self.assertRaises(ValueError):
            self.client.embed('Text')

    def test_output_limit_is_not_silent(self):
        self.client._post.return_value = {'message': {'content': 'partial'}, 'done_reason': 'length'}
        with self.assertRaises(ValueError):
            self.client.summarize('Text')

    def test_unload_request(self):
        self.client.release('summary')
        self.assertEqual(self.client._post.call_args.kwargs['json']['keep_alive'], 0)
        self.client.release('embedding')
        self.assertEqual(self.client._post.call_args.kwargs['json']['input'], [])

    def test_reduction_respects_local_budget(self):
        model = Mock(settings=self.settings)
        model.summarize.return_value = 'Short summary'
        self.assertEqual(summarize_document(['a' * 8000, 'b' * 8000], model), 'Short summary')
        self.assertTrue(all(len(call.args[0]) <= 2816 for call in model.summarize.call_args_list))

    def test_transcription_multipart(self):
        self.client._post.return_value = {'text': 'Hello'}
        self.assertEqual(self.client.transcribe(b'audio', 'test.wav'), 'Hello')
        self.assertEqual(self.client._post.call_args.kwargs['data']['model'], self.settings.transcription_model)
