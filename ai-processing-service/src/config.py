"""Environment configuration. Model URLs are complete endpoint URLs."""
import os
import math
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

@dataclass(frozen=True)
class Settings:
    """Immutable host/container settings; model URLs are complete endpoints."""
    # Infrastructure is shared locally, but the worker owns only its schema.
    database_url: str
    amqp_url: str
    content_url: str
    processing_token: str
    # Keys may be empty for local inference; remote providers can require them.
    summary_url: str
    summary_model: str
    summary_key: str
    embedding_url: str
    embedding_model: str
    embedding_key: str
    transcription_url: str
    transcription_model: str
    transcription_key: str
    timeout: float = 300
    chunk_size: int = 2000
    chunk_overlap: int = 200
    max_attempts: int = 3
    model_provider: str = "ollama"
    ollama_context: int = 4096
    summary_max_tokens: int = 512
    # These safety limits are per processing attempt, not per queue delivery.
    max_chunks: int = 128
    max_model_calls: int = 512
    max_job_seconds: float = 900
    retry_base_seconds: float = 10
    retry_max_seconds: float = 300

    @classmethod
    def load(cls):
        """Read the root .env while allowing injected container values to win."""
        load_dotenv(Path(__file__).resolve().parents[2] / '.env')
        def env(name, default=''):
            return os.getenv(name, default).strip()
        return cls(
            env('STUDYHUB_DATABASE_URL'), env('STUDYHUB_AMQP_URL'),
            env('AI_CONTENT_URL', 'http://127.0.0.1:8000'),
            env('STUDYHUB_PROCESSING_TOKEN'),
            env('AI_SUMMARY_URL'), env('AI_SUMMARY_MODEL'), env('AI_SUMMARY_KEY'),
            env('AI_EMBEDDING_URL'), env('AI_EMBEDDING_MODEL'), env('AI_EMBEDDING_KEY'),
            env('AI_TRANSCRIPTION_URL'), env('AI_TRANSCRIPTION_MODEL'), env('AI_TRANSCRIPTION_KEY'),
            float(env('AI_HTTP_TIMEOUT', '300')), int(env('AI_CHUNK_SIZE', '2000')),
            int(env('AI_CHUNK_OVERLAP', '200')), int(env('AI_MAX_ATTEMPTS', '3')),
            env('AI_MODEL_PROVIDER', 'ollama'), int(env('AI_OLLAMA_CONTEXT', '4096')),
            int(env('AI_SUMMARY_MAX_TOKENS', '512')),
            int(env('AI_MAX_CHUNKS', '128')), int(env('AI_MAX_MODEL_CALLS', '512')),
            float(env('AI_PROCESSING_MAX_JOB_SECONDS', '900')),
            float(env('AI_RETRY_BASE_SECONDS', '10')), float(env('AI_RETRY_MAX_SECONDS', '300')),
        )

    def validate(self):
        """Reject invalid settings before taking ownership of any broker message."""
        required = ['database_url', 'amqp_url', 'content_url', 'processing_token',
                    'summary_url', 'summary_model', 'embedding_url', 'embedding_model']
        missing = [name for name in required if not getattr(self, name)]
        if missing:
            raise ValueError('Missing configuration: ' + ', '.join(missing))
        if not 0 <= self.chunk_overlap < self.chunk_size or self.chunk_size > 10000:
            raise ValueError('Require 0 <= overlap < chunk size <= 10000')
        if self.model_provider not in ('ollama', 'openai'):
            raise ValueError('AI_MODEL_PROVIDER must be ollama or openai')
        if self.ollama_context < 2048 or not 1 <= self.summary_max_tokens <= 1024:
            raise ValueError('Context must be >= 2048 and summary tokens between 1 and 1024')
        if not all(math.isfinite(value) and value > 0 for value in (
            self.timeout, self.max_job_seconds, self.retry_base_seconds, self.retry_max_seconds
        )):
            raise ValueError('Timeouts and retry intervals must be finite positive numbers')
        if not 1 <= self.max_chunks <= 1024 or not 1 <= self.max_model_calls <= 4096:
            raise ValueError('Require 1..1024 chunks and 1..4096 model calls')
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError('Retry base interval must not exceed its cap')
        if self.timeout <= 0 or self.max_attempts < 1:
            raise ValueError('Timeout and max attempts must be positive')
