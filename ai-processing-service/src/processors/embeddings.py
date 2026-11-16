def chunk_text(text, size=2000, overlap=200):
    if not 0 <= overlap < size:
        raise ValueError('Require 0 <= overlap < size')
    chunks = []
    for start in range(0, len(text), size - overlap):
        end = min(start + size, len(text))
        chunks.append({'chunkIndex': len(chunks), 'startOffset': start,
                       'endOffset': end, 'text': text[start:end]})
        if end == len(text):
            break
    return chunks

def embed_chunks(chunks, model_client):
    dimension = None
    for chunk in chunks:
        vector = model_client.embed(chunk['text'])
        if dimension is not None and len(vector) != dimension:
            raise ValueError('Embedding dimensions changed within one document')
        dimension = len(vector)
        chunk['embedding'] = vector
    return chunks
