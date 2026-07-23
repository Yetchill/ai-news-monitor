"""Explicit HTTP failure categories exposed to collection callers."""


class FetchError(Exception):
    """Base error raised while fetching an external resource."""

    def __init__(self, url: str, message: str) -> None:
        self.url = url
        super().__init__(message)


class RetryableFetchError(FetchError):
    """A transient fetch failure eligible for retry."""


class FetchTimeoutError(RetryableFetchError):
    """The remote request exceeded its configured timeout."""


class NetworkFetchError(RetryableFetchError):
    """A transient connection or protocol error occurred."""


class ForbiddenFetchError(FetchError):
    """The server denied access with HTTP 403."""


class NotFoundFetchError(FetchError):
    """The requested resource returned HTTP 404."""


class RateLimitFetchError(RetryableFetchError):
    """The remote service reported an exhausted request allowance."""

    def __init__(self, url: str, message: str, retry_after: str | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(url, message)


class ServerFetchError(RetryableFetchError):
    """The remote service returned a 5xx status."""


class HttpStatusFetchError(FetchError):
    """The remote service returned another non-success status."""

    def __init__(self, url: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(url, f"HTTP {status_code} while fetching {url}")
