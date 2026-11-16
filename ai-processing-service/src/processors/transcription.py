"""Audio processor boundary; model transport is owned by the model client."""

def transcribe(contents, filename, model_client):
    """Preserve the source filename so the remote decoder can identify its format."""
    return model_client.transcribe(contents, filename)
