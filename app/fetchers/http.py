"""Polite asynchronous HTTP fetcher with bounded retries."""

import asyncio
import logging
from collections.abc import Mapping
from time import monotonic
from urllib.parse import urlsplit

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.domain.collection import FetchResult
from app.fetchers.errors import (
    FetchError,
    FetchTimeoutError,
    ForbiddenFetchError,
    HttpStatusFetchError,
    NetworkFetchError,
    NotFoundFetchError,
    RateLimitFetchError,
    ResponseTooLargeFetchError,
    RetryableFetchError,
    ServerFetchError,
)

DEFAULT_USER_AGENT = (
    "AIIntelligenceMonitor/0.2 (+local research aggregator; respectful automated client)"
)
DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
LOGGER = logging.getLogger("app.crawler")


class HttpFetcher:
    """Fetch public HTTP resources without browser emulation or access circumvention."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        request_interval_seconds: float = 1.5,
        max_retries: int = 2,
        per_domain_concurrency: int = 2,
        global_concurrency: int = 5,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if per_domain_concurrency <= 0:
            raise ValueError("per_domain_concurrency must be positive")
        if global_concurrency <= 0:
            raise ValueError("global_concurrency must be positive")
        if max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")

        self._timeout = httpx.Timeout(timeout_seconds)
        self._request_interval = request_interval_seconds
        self._max_retries = max_retries
        self._per_domain_concurrency = per_domain_concurrency
        self._user_agent = user_agent
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self._owns_client = client is None
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}
        self._global_semaphore = asyncio.Semaphore(global_concurrency)
        self._last_request_at: dict[str, float] = {}

    async def __aenter__(self) -> "HttpFetcher":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally-created client."""

        if self._owns_client:
            await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """Fetch a URL, retrying timeouts, network errors, rate limits, and 5xx responses."""

        self._validate_url(url)
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(RetryableFetchError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                domain_semaphore = self._domain_semaphore(url)
                async with domain_semaphore:
                    await self._wait_for_domain(url)
                    async with self._global_semaphore:
                        return await self._fetch_once(url, headers=headers)
        raise AssertionError("tenacity retry loop ended without a result")

    async def post(
        self,
        url: str,
        *,
        body: str,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """Post a JSON body, retrying timeouts, network errors, rate limits, and 5xx responses."""

        self._validate_url(url)
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(RetryableFetchError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                domain_semaphore = self._domain_semaphore(url)
                async with domain_semaphore:
                    await self._wait_for_domain(url)
                    async with self._global_semaphore:
                        return await self._post_once(url, body=body, headers=headers)
        raise AssertionError("tenacity retry loop ended without a result")

    @staticmethod
    def _validate_url(url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("fetch URL must use HTTP or HTTPS")

    async def _wait_for_domain(self, url: str) -> None:
        hostname = (urlsplit(url).hostname or "").lower()
        lock = self._domain_locks.setdefault(hostname, asyncio.Lock())
        async with lock:
            previous = self._last_request_at.get(hostname)
            if previous is not None:
                delay = self._request_interval - (monotonic() - previous)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request_at[hostname] = monotonic()

    def _domain_semaphore(self, url: str) -> asyncio.Semaphore:
        hostname = (urlsplit(url).hostname or "").lower()
        return self._domain_semaphores.setdefault(
            hostname,
            asyncio.Semaphore(self._per_domain_concurrency),
        )

    async def _fetch_once(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
    ) -> FetchResult:
        request_headers = {"User-Agent": self._user_agent}
        if headers:
            request_headers.update(headers)
        try:
            response, content = await self._send_bounded(
                "GET",
                url,
                headers=request_headers,
            )
        except httpx.TimeoutException as error:
            raise FetchTimeoutError(url, f"Timeout while fetching {url}") from error
        except httpx.RequestError as error:
            raise NetworkFetchError(url, f"Network error while fetching {url}: {error}") from error

        status = response.status_code
        response_url = str(response.url)
        if status == 403:
            if _is_rate_limit_response(response, content):
                raise RateLimitFetchError(
                    response_url,
                    f"Rate limit exhausted while fetching {response_url}",
                    response.headers.get("retry-after")
                    or response.headers.get("x-ratelimit-reset"),
                )
            raise ForbiddenFetchError(response_url, f"HTTP 403 while fetching {response_url}")
        if status == 404:
            raise NotFoundFetchError(response_url, f"HTTP 404 while fetching {response_url}")
        if status == 429:
            raise RateLimitFetchError(
                response_url,
                f"HTTP 429 while fetching {response_url}",
                response.headers.get("retry-after"),
            )
        if 500 <= status <= 599:
            raise ServerFetchError(response_url, f"HTTP {status} while fetching {response_url}")
        if status >= 400:
            raise HttpStatusFetchError(response_url, status)

        return FetchResult(
            requested_url=url,
            url=response_url,
            status_code=status,
            headers=dict(response.headers.items()),
            content=content,
            encoding=response.encoding or "utf-8",
        )

    async def _post_once(
        self,
        url: str,
        *,
        body: str,
        headers: Mapping[str, str] | None,
    ) -> FetchResult:
        request_headers = {"User-Agent": self._user_agent}
        if headers:
            request_headers.update(headers)
        try:
            response, content = await self._send_bounded(
                "POST",
                url,
                body=body,
                headers=request_headers,
            )
        except httpx.TimeoutException as error:
            raise FetchTimeoutError(url, f"Timeout while posting to {url}") from error
        except httpx.RequestError as error:
            msg = f"Network error while posting to {url}: {error}"
            raise NetworkFetchError(url, msg) from error

        status = response.status_code
        response_url = str(response.url)
        if status == 403:
            if _is_rate_limit_response(response, content):
                raise RateLimitFetchError(
                    response_url,
                    f"Rate limit exhausted while posting to {response_url}",
                    response.headers.get("retry-after")
                    or response.headers.get("x-ratelimit-reset"),
                )
            raise ForbiddenFetchError(response_url, f"HTTP 403 while posting to {response_url}")
        if status == 404:
            raise NotFoundFetchError(response_url, f"HTTP 404 while posting to {response_url}")
        if status == 429:
            raise RateLimitFetchError(
                response_url,
                f"HTTP 429 while posting to {response_url}",
                response.headers.get("retry-after"),
            )
        if 500 <= status <= 599:
            raise ServerFetchError(response_url, f"HTTP {status} while posting to {response_url}")
        if status >= 400:
            raise HttpStatusFetchError(response_url, status)

        return FetchResult(
            requested_url=url,
            url=response_url,
            status_code=status,
            headers=dict(response.headers.items()),
            content=content,
            encoding=response.encoding or "utf-8",
        )

    async def _send_bounded(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: str | None = None,
    ) -> tuple[httpx.Response, bytes]:
        request = self._client.build_request(
            method,
            url,
            headers=headers,
            content=body,
            timeout=self._timeout,
        )
        response = await self._client.send(request, stream=True, follow_redirects=True)
        try:
            declared = response.headers.get("content-length")
            if declared and declared.isdecimal() and int(declared) > self._max_response_bytes:
                LOGGER.warning(
                    "Response too large for %s: declared=%s limit=%s",
                    response.url,
                    declared,
                    self._max_response_bytes,
                )
                raise ResponseTooLargeFetchError(
                    str(response.url), "Response exceeded the configured size limit"
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    LOGGER.warning(
                        "Response too large for %s: streamed>%s",
                        response.url,
                        self._max_response_bytes,
                    )
                    raise ResponseTooLargeFetchError(
                        str(response.url), "Response exceeded the configured size limit"
                    )
                chunks.append(chunk)
            return response, b"".join(chunks)
        finally:
            await response.aclose()


def _is_rate_limit_response(response: httpx.Response, content: bytes = b"") -> bool:
    """Distinguish retryable HTTP 403 rate limits from ordinary access denials."""

    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    if response.headers.get("retry-after") is not None:
        return True
    hostname = (response.url.host or "").lower()
    if hostname != "api.github.com":
        return False
    message = content[:2000].decode(response.encoding or "utf-8", errors="replace").casefold()
    return "rate limit" in message or "secondary rate" in message


__all__ = [
    "FetchError",
    "FetchTimeoutError",
    "ForbiddenFetchError",
    "HttpFetcher",
    "HttpStatusFetchError",
    "NetworkFetchError",
    "NotFoundFetchError",
    "RateLimitFetchError",
    "ResponseTooLargeFetchError",
    "ServerFetchError",
]
