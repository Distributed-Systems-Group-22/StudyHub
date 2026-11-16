"""Validate source identity only; this does not verify semantic support."""
import re

from app.services.errors import InvalidCitation


def source_catalog(chunks):
    return {f"S{index}": chunk for index, chunk in enumerate(chunks, 1)}


def validate_citations(text, catalog, *, require=True):
    # Prompt accepts only [S1] syntax. Reject other bracketed source claims too.
    references = re.findall(r"\[([^\[\]\n]*)\]", text)
    if text.count("[") != len(references) or text.count("]") != len(references):
        raise InvalidCitation("Malformed citation brackets")
    if any(reference not in catalog for reference in references):
        raise InvalidCitation("Model cited a source outside the supplied catalog")
    if require and not references:
        raise InvalidCitation("Model returned no traceable citations")
    citations = []
    for reference in dict.fromkeys(references):
        chunk = catalog[reference]
        citations.append({
            "source_id": reference,
            "document_id": chunk.document_id,
            "document_version": chunk.document_version,
            "chunk_id": chunk.chunk_id,
            "page": chunk.page,
            "source_location": chunk.source_location,
        })
    return citations
