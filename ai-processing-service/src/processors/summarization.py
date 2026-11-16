"""Hierarchical summarization with bounded input groups and reduction depth."""
def summarize_document(chunks, model_client):
    """Reduce through bounded text groups, including summaries longer than one group."""
    settings = model_client.settings
    budget = (settings.ollama_context - settings.summary_max_tokens - 768
              if settings.model_provider == 'ollama' else 10000)
    if budget < 256:
        raise ValueError('Summary context budget is too small')

    def split(text):
        """Avoid silently dropping text when an intermediate summary is too large."""
        return [text[i:i + budget] for i in range(0, len(text), budget)]

    summaries = [model_client.summarize(part) for chunk in chunks for part in split(chunk)]
    if not summaries:
        raise ValueError('No text to summarize')
    for _ in range(12):
        if len(summaries) == 1:
            return summaries[0]
        combined = '\n\n'.join(summaries)
        summaries = [model_client.summarize(part) for part in split(combined)]
    raise ValueError('Summary reduction did not converge')
