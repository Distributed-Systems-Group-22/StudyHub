from pathlib import Path
from src.processors.extraction import AUDIO_EXTENSIONS, extract_text
from src.processors.transcription import transcribe
from src.processors.summarization import summarize_document
from src.processors.embeddings import chunk_text, embed_chunks

class Pipeline:
    def __init__(self, settings, content_client, model_client):
        self.settings = settings
        self.content = content_client
        self.models = model_client

    def process(self, document_id, version):
        contents, filename = self.content.download(document_id, version)
        text = (transcribe(contents, filename, self.models)
                if Path(filename).suffix.lower() in AUDIO_EXTENSIONS
                else extract_text(contents, filename))
        if len(text) > 200000:
            raise ValueError('Extracted text exceeds the initial 200000-character limit')
        chunks = chunk_text(text, self.settings.chunk_size, self.settings.chunk_overlap)
        try:
            summary = summarize_document([chunk['text'] for chunk in chunks], self.models)
        finally:
            self.models.release('summary')
        try:
            embedded = embed_chunks(chunks, self.models)
        finally:
            self.models.release('embedding')
        return {'documentId': document_id, 'documentVersion': version,
                'summary': summary, 'embeddingModel': self.settings.embedding_model,
                'summaryModel': self.settings.summary_model,
                'chunks': embedded}
