"""Authenticated, version-specific access to Content-owned files and status."""
from email.message import Message
import httpx

class DocumentUnavailable(Exception):
    """Deleted, missing or obsolete document version."""

class ContentClient:
    """Use the Processing token without impersonating an end user."""
    def __init__(self, settings):
        self.settings = settings

    def _request(self, method, document_id, version, suffix, **kwargs):
        """Map unavailable versions to cancellation; propagate other HTTP failures."""
        s = self.settings
        url = f'{s.content_url.rstrip("/")}/internal/documents/{document_id}/versions/{version}/{suffix}'
        with httpx.Client(timeout=s.timeout) as client:
            response = client.request(method, url,
                headers={'Authorization': f'Bearer {s.processing_token}'}, **kwargs)
        if response.status_code == 404:
            raise DocumentUnavailable()
        response.raise_for_status()
        return response

    def download(self, document_id, version):
        """Recover the original filename from headers for processor selection."""
        response = self._request('GET', document_id, version, 'file')
        disposition = Message()
        disposition['content-disposition'] = response.headers.get('content-disposition', '')
        filename = disposition.get_filename()
        if not filename:
            raise ValueError('Content response has no original filename')
        if len(response.content) > 5 * 1024 * 1024:
            raise ValueError('File exceeds the Content upload limit')
        return response.content, filename

    def report(self, document_id, version, status):
        """Report only final states; Content enforces its terminal-state rules."""
        self._request('PUT', document_id, version, 'processing-status', json={'status': status})
