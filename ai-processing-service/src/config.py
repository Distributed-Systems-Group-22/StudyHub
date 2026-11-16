"""Environment configuration. Model URLs are complete endpoint URLs."""
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

@dataclass(frozen=True)
class Settings:
    database_url: str
    amqp_url: str
    content_url: str
    processing_token: str
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

    @classmethod
    def load(cls):
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
        )

    def validate(self):
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
        if self.timeout <= 0 or self.max_attempts < 1:
            raise ValueError('Timeout and max attempts must be positive')
