def transcribe(contents, filename, model_client):
    return model_client.transcribe(contents, filename)
