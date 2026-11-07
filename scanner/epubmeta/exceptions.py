"""Custom exceptions for epubmeta."""


class InvalidDocumentError(Exception):
    """Raised when an EPUB or PDF file is corrupt, malformed, or unreadable."""
    pass


class DrmProtectedError(Exception):
    """Raised when an EPUB or PDF file is DRM-locked or password-encrypted (not corrupt)."""
    pass



class RateLimitExceededError(Exception):
    """Raised when an external API or AI provider hits a rate/quota limit (HTTP 429 / RESOURCE_EXHAUSTED)."""

    def __init__(self, provider: str, message: str = "", retry_after: float | None = None):
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(message or f"{provider} rate/quota limit reached (429)")

