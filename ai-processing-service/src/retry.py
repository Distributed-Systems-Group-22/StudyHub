"""Classify recoverable outages and calculate bounded retry delays."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math

import httpx


def is_transient(error):
    """Keep network outages and provider throttling out of the failure budget."""
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, httpx.HTTPStatusError) and (
        error.response.status_code in (408, 429) or error.response.status_code >= 500
    )


def retry_delay(retry_count, error, base=10, cap=300):
    """Use exponential backoff, respecting numeric or HTTP-date Retry-After."""
    delay = min(cap, base * 2 ** min(retry_count, 16))
    if isinstance(error, httpx.HTTPStatusError):
        value = error.response.headers.get('Retry-After', '')
        try:
            requested = float(value)
        except ValueError:
            try:
                requested = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                requested = 0
        if math.isfinite(requested):
            # Bound hostile or mistaken headers while allowing a long cooldown.
            delay = max(delay, min(max(requested, 0), 86400))
    return delay
