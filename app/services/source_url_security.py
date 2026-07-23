"""SSRF-resistant URL validation and bounded fetching for source onboarding."""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from time import monotonic
from typing import cast
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from app.domain.collection import FetchResult
from app.fetchers.errors import (
    FetchError,
    FetchTimeoutError,
    ForbiddenFetchError,
    NotFoundFetchError,
    RateLimitFetchError,
    ServerFetchError,
)
from app.fetchers.http import DEFAULT_USER_AGENT
from app.utils.url import canonicalize_url

MAX_SOURCE_URL_LENGTH = 2048
SAFE_PORTS = {"http": {80}, "https": {443}}
MAX_REDIRECTS = 4
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SAFE_REQUEST_HEADERS = {"accept", "user-agent", "x-github-api-version"}
AMBIGUOUS_NUMERIC_HOST = re.compile(
    r"(?i)^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}$"
)

Resolver = Callable[[str], Awaitable[Sequence[str]]]


class SourceUrlSecurityError(ValueError):
    """A user-facing URL rejection that never includes local addressing details."""


class ResponseTooLargeFetchError(FetchError):
    """The response exceeded the onboarding byte limit."""


async def resolve_public_addresses(hostname: str) -> Sequence[str]:
    """Resolve both IPv4 and IPv6 without blocking the event loop."""

    def resolve() -> Sequence[str]:
        rows = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(str(row[4][0]) for row in rows))

    try:
        return await asyncio.to_thread(resolve)
    except OSError as exc:
        raise SourceUrlSecurityError("无法解析该网址的主机名。") from exc


class SourceUrlGuard:
    """Normalize input and require every resolved target to be globally routable."""

    def __init__(self, resolver: Resolver = resolve_public_addresses) -> None:
        self._resolver = resolver

    def normalize(self, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise SourceUrlSecurityError("请输入来源网址。")
        if len(candidate) > MAX_SOURCE_URL_LENGTH:
            raise SourceUrlSecurityError("网址过长, 请使用不超过 2048 个字符的地址。")
        if any(character in candidate for character in ("\\", "\r", "\n", "\t")):
            raise SourceUrlSecurityError("网址格式无效。")
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError as exc:
            raise SourceUrlSecurityError("网址格式无效。") from exc
        scheme = parts.scheme.lower()
        if scheme not in SAFE_PORTS or not parts.hostname:
            raise SourceUrlSecurityError("只允许使用 HTTP 或 HTTPS 网址。")
        if parts.username is not None or parts.password is not None:
            raise SourceUrlSecurityError("网址中不能包含用户名或密码。")
        effective_port = port or (80 if scheme == "http" else 443)
        if effective_port not in SAFE_PORTS[scheme]:
            raise SourceUrlSecurityError("该网址端口不在允许的安全端口范围内。")
        normalized = canonicalize_url(candidate)
        if normalized is None or len(normalized) > MAX_SOURCE_URL_LENGTH:
            raise SourceUrlSecurityError("网址格式无效。")
        normalized_host = urlsplit(normalized).hostname or ""
        if "%" in normalized_host or (
            _parse_address(normalized_host) is None
            and AMBIGUOUS_NUMERIC_HOST.fullmatch(normalized_host)
        ):
            raise SourceUrlSecurityError("网址主机名格式无效。")
        return normalized

    async def validate(self, value: str) -> str:
        normalized = self.normalize(value)
        hostname = urlsplit(normalized).hostname or ""
        await self.resolve_addresses(hostname)
        return normalized

    async def resolve_addresses(self, hostname: str) -> tuple[str, ...]:
        """Resolve once and return only an all-public address set."""

        lowered = hostname.casefold().rstrip(".")
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise SourceUrlSecurityError("该网址指向不允许访问的本地或内部地址。")
        literal = _parse_address(lowered)
        addresses = (str(literal),) if literal is not None else await self._resolver(lowered)
        if not addresses:
            raise SourceUrlSecurityError("无法解析该网址的主机名。")
        validated: list[str] = []
        for value in addresses:
            address = _parse_address(value)
            if address is None or not address.is_global:
                raise SourceUrlSecurityError("该网址指向不允许访问的本地或内部地址。")
            rendered = str(address)
            if rendered not in validated:
                validated.append(rendered)
        return tuple(validated)


class PublicAddressNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve at connect time, validate every answer, then dial a validated IP literal."""

    def __init__(
        self,
        guard: SourceUrlGuard,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._backend = backend or cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if port not in {80, 443}:
            raise SourceUrlSecurityError("该网址端口不在允许的安全端口范围内。")
        addresses = await self._guard.resolve_addresses(host)
        deadline = monotonic() + timeout if timeout is not None else None
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            remaining = None if deadline is None else max(0.001, deadline - monotonic())
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("public target did not provide a usable address")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("Unix sockets are not allowed for source onboarding")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose TCP backend cannot perform an unchecked DNS lookup."""

    def __init__(
        self,
        guard: SourceUrlGuard,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(  # pyright: ignore[reportPrivateUsage]
            network_backend=PublicAddressNetworkBackend(guard, network_backend),
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=5.0,
        )


class SafeHttpFetcher:
    """Fetcher used only by discovery/preview with per-hop validation and body limits."""

    def __init__(
        self,
        guard: SourceUrlGuard,
        *,
        timeout_seconds: float = 10.0,
        max_redirects: int = MAX_REDIRECTS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_redirects = max(0, min(max_redirects, MAX_REDIRECTS))
        self._max_response_bytes = max(1024, min(max_response_bytes, MAX_RESPONSE_BYTES))
        resolved_transport = transport or _PinnedAsyncHTTPTransport(guard, network_backend)
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            trust_env=False,
            transport=resolved_transport,
        )

    async def __aenter__(self) -> "SafeHttpFetcher":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        requested_url = self._guard.normalize(url)
        current_url = requested_url
        safe_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.casefold() in SAFE_REQUEST_HEADERS
        }
        for redirect_count in range(self._max_redirects + 1):
            # Validate the exact redirect target but keep its path spelling for the request.
            # Some servers redirect a canonicalized ``/feed`` back to ``/feed/``.
            await self._guard.validate(current_url)
            try:
                request = self._client.build_request(
                    "GET",
                    current_url,
                    headers=safe_headers,
                    timeout=self._timeout,
                )
                request.headers.pop("cookie", None)
                request.headers.pop("authorization", None)
                request.headers.pop("proxy-authorization", None)
                response = await self._client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect_count >= self._max_redirects:
                            raise FetchError(current_url, "网页重定向次数过多或目标无效。")
                        redirect_url = urljoin(current_url, location)
                        if (
                            urlsplit(current_url).scheme.casefold() == "https"
                            and urlsplit(redirect_url).scheme.casefold() == "http"
                        ):
                            raise SourceUrlSecurityError("不允许从 HTTPS 降级重定向到 HTTP。")
                        current_url = redirect_url
                        continue
                    status = response.status_code
                    if status == 403:
                        if response.headers.get("x-ratelimit-remaining") == "0":
                            raise RateLimitFetchError(current_url, "公开接口请求配额暂时耗尽。")
                        raise ForbiddenFetchError(current_url, "网站拒绝公开访问。")
                    if status == 404:
                        raise NotFoundFetchError(current_url, "网页不存在。")
                    if status == 429:
                        raise RateLimitFetchError(current_url, "网站请求频率受限。")
                    if 500 <= status <= 599:
                        raise ServerFetchError(current_url, "网站服务暂时不可用。")
                    if status >= 400:
                        raise FetchError(current_url, f"网页返回 HTTP {status}, 无法用于预览。")
                    declared = response.headers.get("content-length")
                    if (
                        declared
                        and declared.isdecimal()
                        and int(declared) > self._max_response_bytes
                    ):
                        raise ResponseTooLargeFetchError(
                            current_url, "网页响应内容超过预览大小限制。"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_response_bytes:
                            raise ResponseTooLargeFetchError(
                                current_url, "网页响应内容超过预览大小限制。"
                            )
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    return FetchResult(
                        requested_url=requested_url,
                        url=str(response.url),
                        status_code=response.status_code,
                        headers=dict(response.headers.items()),
                        content=b"".join(chunks),
                        encoding=encoding,
                    )
                finally:
                    await response.aclose()
            except httpx.TimeoutException as exc:
                raise FetchTimeoutError(current_url, "网页响应超时, 请稍后重试。") from exc
            except httpx.RequestError as exc:
                raise FetchError(current_url, "无法安全访问该网页。") from exc
        raise FetchError(current_url, "网页重定向次数过多。")

    async def post(
        self,
        url: str,
        *,
        body: str,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """Secure POST with the same URL validation as fetch. No redirects—API endpoints only."""
        requested_url = self._guard.normalize(url)
        await self._guard.validate(requested_url)
        safe_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.casefold() in SAFE_REQUEST_HEADERS
        }
        try:
            request = self._client.build_request(
                "POST",
                requested_url,
                content=body,
                headers=safe_headers,
                timeout=self._timeout,
            )
            request.headers.pop("cookie", None)
            request.headers.pop("authorization", None)
            request.headers.pop("proxy-authorization", None)
            response = await self._client.send(request, stream=True, follow_redirects=False)
            try:
                status = response.status_code
                if status == 403:
                    if response.headers.get("x-ratelimit-remaining") == "0":
                        raise RateLimitFetchError(requested_url, "公开接口请求配额暂时耗尽。")
                    raise ForbiddenFetchError(requested_url, "网站拒绝公开访问。")
                if status == 404:
                    raise NotFoundFetchError(requested_url, "网页不存在。")
                if status == 429:
                    raise RateLimitFetchError(requested_url, "网站请求频率受限。")
                if 500 <= status <= 599:
                    raise ServerFetchError(requested_url, "网站服务暂时不可用。")
                if status >= 400:
                    raise FetchError(requested_url, f"网页返回 HTTP {status}, 无法用于预览。")
                declared = response.headers.get("content-length")
                if (
                    declared
                    and declared.isdecimal()
                    and int(declared) > self._max_response_bytes
                ):
                    raise ResponseTooLargeFetchError(
                        requested_url, "网页响应内容超过预览大小限制。"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise ResponseTooLargeFetchError(
                            requested_url, "网页响应内容超过预览大小限制。"
                        )
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return FetchResult(
                    requested_url=requested_url,
                    url=str(response.url),
                    status_code=response.status_code,
                    headers=dict(response.headers.items()),
                    content=b"".join(chunks),
                    encoding=encoding,
                )
            finally:
                await response.aclose()
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError(requested_url, "网页响应超时, 请稍后重试。") from exc
        except httpx.RequestError as exc:
            raise FetchError(requested_url, "无法安全访问该网页。") from exc


def _parse_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


__all__ = [
    "MAX_RESPONSE_BYTES",
    "MAX_SOURCE_URL_LENGTH",
    "PublicAddressNetworkBackend",
    "ResponseTooLargeFetchError",
    "SafeHttpFetcher",
    "SourceUrlGuard",
    "SourceUrlSecurityError",
]
