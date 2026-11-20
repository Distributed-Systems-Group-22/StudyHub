"""Cross-platform faster-whisper HTTP bridge for the Docker-compatible processing worker.
Each transcription runs in a child process so exiting releases decoder memory.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

load_dotenv(Path(__file__).resolve().parents[2] / '.env')
MODEL = os.getenv('AI_TRANSCRIPTION_MODEL', 'small').strip()
TOKEN = os.getenv('AI_TRANSCRIPTION_KEY', '').strip()
TIMEOUT = float(os.getenv('AI_HTTP_TIMEOUT', '300'))
MAX_BYTES = 5 * 1024 * 1024
lock = threading.Lock()
app = FastAPI(title='StudyHub Faster Whisper Transcription')


@app.get('/health')
def health():
    """Expose process liveness without allocating model memory."""
    return {'status': 'ok', 'backend': 'faster-whisper', 'model': MODEL}


def transcribe_file(path):
    """Allowlist the configured model; do not accept arbitrary client model paths."""
    with tempfile.TemporaryDirectory(prefix='studyhub-text-') as directory:
        output = Path(directory) / 'result.json'
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).with_name('transcribe_file.py')),
             path, MODEL, str(output)], capture_output=True, timeout=TIMEOUT,
        )
        if completed.returncode != 0:
            raise RuntimeError('Transcription process failed; check model cache and faster-whisper dependencies')
        text = json.loads(output.read_text(encoding='utf-8'))['text']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('No speech text was produced')
        return text.strip()


@app.post('/v1/audio/transcriptions')
def transcribe(file: UploadFile = File(...), model: str = Form(...),
               authorization: str | None = Header(default=None)):
    """Authenticate, serialize decoding, and remove all temporary upload artifacts."""
    if TOKEN and not secrets.compare_digest((authorization or '').encode(), f'Bearer {TOKEN}'.encode()):
        raise HTTPException(401, 'Invalid transcription token')
    if model != MODEL:
        raise HTTPException(400, 'Requested model differs from configured transcription model')
    if not lock.acquire(blocking=False):
        raise HTTPException(429, 'Transcription busy; retry later', headers={'Retry-After': '10'})
    try:
        contents = file.file.read(MAX_BYTES + 1)
        if not contents or len(contents) > MAX_BYTES:
            raise HTTPException(400, 'Audio must be nonempty and at most 5 MiB')
        suffix = Path(file.filename or 'audio.wav').suffix.lower()
        if suffix not in {'.mp3', '.wav', '.m4a', '.mp4', '.ogg', '.webm', '.flac'}:
            raise HTTPException(400, 'Unsupported audio extension')
        with tempfile.TemporaryDirectory(prefix='studyhub-audio-') as directory:
            path = Path(directory) / f'audio{suffix}'
            path.write_bytes(contents)
            return {'text': transcribe_file(str(path))}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, 'Transcription timed out') from None
    except (ValueError, RuntimeError):
        raise HTTPException(503, 'Transcription failed; verify faster-whisper dependencies, model cache and audio') from None
    finally:
        lock.release()
        file.file.close()


if __name__ == '__main__':
    import uvicorn
    bind = os.getenv('AI_TRANSCRIPTION_BIND', '127.0.0.1')
    if bind not in ('127.0.0.1', 'localhost', '::1') and not TOKEN:
        raise SystemExit('Set AI_TRANSCRIPTION_KEY before binding beyond loopback')
    uvicorn.run(app, host=bind, port=int(os.getenv('AI_TRANSCRIPTION_PORT', '8001')))
