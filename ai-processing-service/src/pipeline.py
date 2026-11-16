"""Coordinate one bounded document attempt without owning delivery state."""
from pathlib import Path
from src.budget import TaskBudget, BudgetedModels
from src.processors.extraction import AUDIO_EXTENSIONS, extract_text
from src.processors.transcription import transcribe
from src.processors.summarization import summarize_document
from src.processors.embeddings import chunk_text, embed_chunks

class Pipeline:
    """Return a complete derived artifact; the delivery handler persists and publishes it."""
    def __init__(self, settings, content_client, model_client):
        self.settings = settings
        self.content = content_client
        self.models = model_client

    def process(self, document_id, version):
        """Run extraction, summary and embedding phases under one shared budget."""
        budget = TaskBudget(getattr(self.settings, 'max_model_calls', 512),
                            getattr(self.settings, 'max_job_seconds', 900))
        models = BudgetedModels(self.models, budget)
        contents, filename = self.content.download(document_id, version)
        text = (transcribe(contents, filename, models)
                if Path(filename).suffix.lower() in AUDIO_EXTENSIONS
                else extract_text(contents, filename))
        budget.remaining()
        if len(text) > 200000:
            raise ValueError('Extracted text exceeds the initial 200000-character limit')
        chunks = chunk_text(text, self.settings.chunk_size, self.settings.chunk_overlap,
                            getattr(self.settings, 'max_chunks', 128))
        try:
            summary = summarize_document([chunk['text'] for chunk in chunks], models)
        finally:
            models.release('summary')
        try:
            embedded = embed_chunks(chunks, models)
        finally:
            models.release('embedding')
        return {'documentId': document_id, 'documentVersion': version,
                'summary': summary, 'embeddingModel': self.settings.embedding_model,
                'summaryModel': self.settings.summary_model,
                'chunks': embedded}
