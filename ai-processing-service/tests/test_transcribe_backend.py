"""Exercise faster-whisper integration without loading a real model or GPU."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class TranscriptionBackendTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'native/transcribe_file.py'
        spec = importlib.util.spec_from_file_location('transcribe_backend', path)
        self.backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.backend)

    def run_backend(self, segments, env=None, model='small'):
        decoder = Mock()
        decoder.transcribe.return_value = (iter(segments), SimpleNamespace(language='en'))
        factory = Mock(return_value=decoder)
        with patch.dict('sys.modules', {'faster_whisper': SimpleNamespace(WhisperModel=factory)}):
            with patch.dict(os.environ, env or {}, clear=True):
                text = self.backend.transcribe_audio('fixture.wav', model)
        return text, factory, decoder

    def test_cpu_int8_default_and_lazy_segments(self):
        segments = [SimpleNamespace(text=' Hello '), SimpleNamespace(text=''), SimpleNamespace(text='world')]
        text, factory, decoder = self.run_backend(segments)
        self.assertEqual(text, 'Hello world')
        factory.assert_called_once_with('small', device='cpu', compute_type='int8', cpu_threads=4, num_workers=1)
        self.assertIsNone(decoder.transcribe.call_args.kwargs['language'])
        self.assertEqual(decoder.transcribe.call_args.kwargs['task'], 'transcribe')

    def test_cuda_and_language_overrides(self):
        _, factory, decoder = self.run_backend([SimpleNamespace(text='你好')], {
            'AI_TRANSCRIPTION_DEVICE': 'cuda', 'AI_TRANSCRIPTION_COMPUTE_TYPE': 'float16',
            'AI_TRANSCRIPTION_CPU_THREADS': '2', 'AI_TRANSCRIPTION_LANGUAGE': 'zh'})
        self.assertEqual(factory.call_args.kwargs['device'], 'cuda')
        self.assertEqual(decoder.transcribe.call_args.kwargs['language'], 'zh')

    def test_empty_transcript_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_backend([])

    def test_legacy_mlx_checkpoint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'MLX checkpoints'):
            self.run_backend([], model='mlx-community/whisper-small-mlx')

    def test_invalid_thread_configuration(self):
        with self.assertRaises(ValueError):
            self.run_backend([], {'AI_TRANSCRIPTION_CPU_THREADS': '0'})
