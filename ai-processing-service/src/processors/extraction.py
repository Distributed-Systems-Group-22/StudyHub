from io import BytesIO
from pathlib import Path

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.ogg', '.webm', '.flac'}

def extract_text(contents, filename):
    suffix = Path(filename).suffix.lower()
    if suffix in {'.txt', '.md', '.csv'}:
        text = contents.decode('utf-8-sig')
    elif suffix == '.pdf':
        from pypdf import PdfReader
        text = '\n\n'.join(page.extract_text() or '' for page in PdfReader(BytesIO(contents)).pages)
    elif suffix == '.docx':
        from docx import Document
        document = Document(BytesIO(contents))
        parts = [p.text for p in document.paragraphs]
        parts.extend(' | '.join(cell.text for cell in row.cells)
                     for table in document.tables for row in table.rows)
        text = '\n'.join(parts)
    else:
        raise ValueError(f'Unsupported document type: {suffix}')
    if not text.strip():
        raise ValueError('No text extracted; scanned documents require OCR, which is not implemented')
    return text.strip()
