"""Split source text into stable, bounded chunks and attach dense embeddings."""

def chunk_text(text, size=2000, overlap=200, max_chunks=128):
    """Validate chunk count before allocating slices or starting model calls."""
    if not 0 <= overlap < size:
        raise ValueError('Require 0 <= overlap < size')
    count = 0 if not text else 1 + max(0, (len(text) - size + size - overlap - 1) // (size - overlap))
    if count > max_chunks:
        raise ValueError(f'Document requires {count} chunks; limit is {max_chunks}')
    chunks = []
    for start in range(0, len(text), size - overlap):
        end = min(start + size, len(text))
        chunks.append({'chunkIndex': len(chunks), 'startOffset': start,
                       'endOffset': end, 'text': text[start:end]})
        if end == len(text):
            break
    return chunks

def embed_chunks(chunks, model_client):
    """Preserve chunk identities and reject mixed embedding dimensions."""
    dimension = None
    for chunk in chunks:
        vector = model_client.embed(chunk['text'])
        if dimension is not None and len(vector) != dimension:
            raise ValueError('Embedding dimensions changed within one document')
        dimension = len(vector)
        chunk['embedding'] = vector
    return chunks
