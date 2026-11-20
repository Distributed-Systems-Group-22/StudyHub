"""Optional bridge tests: install native Web dependencies, model weights not needed."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, 'Native Web dependencies not installed')
class NativeTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'native/transcription_server.py'
        spec = importlib.util.spec_from_file_location('transcription_server', path)
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        self.server.TOKEN = 'test-token'
        self.client = TestClient(self.server.app)

    def request(self, **kwargs):
        return self.client.post('/v1/audio/transcriptions',
            files={'file': ('sample.wav', b'audio', 'audio/wav')},
            data={'model': self.server.MODEL}, **kwargs)

    def test_authentication(self):
        self.assertEqual(self.request().status_code, 401)

    def test_success_and_cleanup(self):
        captured = []
        def fake(path):
            captured.append(path)
            self.assertTrue(Path(path).is_file())
            return 'Transcribed text'
        with patch.object(self.server, 'transcribe_file', side_effect=fake):
            response = self.request(headers={'Authorization': 'Bearer test-token'})
        self.assertEqual(response.json(), {'text': 'Transcribed text'})
        self.assertFalse(Path(captured[0]).exists())
        self.assertFalse(self.server.lock.locked())

    def test_timeout_releases_lock(self):
        with patch.object(self.server, 'transcribe_file', side_effect=subprocess.TimeoutExpired('faster-whisper', 1)):
            response = self.request(headers={'Authorization': 'Bearer test-token'})
        self.assertEqual(response.status_code, 504)
        self.assertFalse(self.server.lock.locked())

    def test_busy_returns_retry_after(self):
        self.server.lock.acquire()
        try:
            response = self.request(headers={'Authorization': 'Bearer test-token'})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['retry-after'], '10')
        finally:
            self.server.lock.release()
