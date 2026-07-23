"""HttpFetcher status categories and retry behavior."""

import asyncio
from collections.abc import Callable
from time import monotonic

import httpx
import pytest

from app.fetchers.errors import (
    FetchTimeoutError,
    ForbiddenFetchError,
    NotFoundFetchError,
    RateLimitFetchError,
)
from app.fetchers.http import HttpFetcher


@pytest.mark.asyncio
async def test_fetcher_sets_user_agent_and_returns_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"].startswith("AIIntelligenceMonitor/")
        return httpx.Response(200, text="ok", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0)
        result = await fetcher.fetch("https://example.com/feed")

    assert result.text == "ok"
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_fetcher_retries_5xx_with_exponential_policy() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts < 3 else 200, text="ready", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0, max_retries=2)
        result = await fetcher.fetch("https://example.com/api")

    assert attempts == 3
    assert result.text == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "expected_error"),
    [
        (403, {}, ForbiddenFetchError),
        (404, {}, NotFoundFetchError),
        (429, {"Retry-After": "60"}, RateLimitFetchError),
        (403, {"X-RateLimit-Remaining": "0"}, RateLimitFetchError),
    ],
)
async def test_fetcher_distinguishes_status_failures(
    status: int,
    headers: dict[str, str],
    expected_error: type[Exception],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0, max_retries=0)
        with pytest.raises(expected_error):
            await fetcher.fetch("https://example.com/resource")


@pytest.mark.asyncio
async def test_fetcher_distinguishes_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    transport_handler: Callable[[httpx.Request], httpx.Response] = handler
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0, max_retries=0)
        with pytest.raises(FetchTimeoutError):
            await fetcher.fetch("https://example.com/slow")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_error"),
    [(403, ForbiddenFetchError), (404, NotFoundFetchError)],
)
async def test_fetcher_does_not_retry_permanent_client_errors(
    status: int,
    expected_error: type[Exception],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0, max_retries=2)
        with pytest.raises(expected_error):
            await fetcher.fetch("https://example.com/resource")

    assert attempts == 1


@pytest.mark.asyncio
async def test_fetcher_recognizes_github_secondary_rate_limit_without_blind_403_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit."},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0, max_retries=0)
        with pytest.raises(RateLimitFetchError):
            await fetcher.fetch("https://api.github.com/repos/example/project/releases")


@pytest.mark.asyncio
async def test_fetcher_serializes_same_domain_request_start_interval_under_concurrency() -> None:
    request_started_at: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_started_at.append(monotonic())
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0.05)
        await asyncio.gather(
            fetcher.fetch("https://example.com/first"),
            fetcher.fetch("https://example.com/second"),
        )

    assert len(request_started_at) == 2
    assert request_started_at[1] - request_started_at[0] >= 0.04


@pytest.mark.asyncio
async def test_fetcher_enforces_same_domain_concurrency_limit() -> None:
    active_requests = 0
    maximum_active_requests = 0
    started_requests = 0
    two_started = asyncio.Event()
    release_requests = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, maximum_active_requests, started_requests
        active_requests += 1
        started_requests += 1
        maximum_active_requests = max(maximum_active_requests, active_requests)
        if started_requests == 2:
            two_started.set()
        await release_requests.wait()
        active_requests -= 1
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0)
        tasks = [
            asyncio.create_task(fetcher.fetch(f"https://example.com/item/{index}"))
            for index in range(4)
        ]
        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert started_requests == 2
        release_requests.set()
        await asyncio.gather(*tasks)

    assert maximum_active_requests == 2


@pytest.mark.asyncio
async def test_fetcher_enforces_global_concurrency_limit_across_domains() -> None:
    active_requests = 0
    maximum_active_requests = 0
    started_requests = 0
    five_started = asyncio.Event()
    release_requests = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, maximum_active_requests, started_requests
        active_requests += 1
        started_requests += 1
        maximum_active_requests = max(maximum_active_requests, active_requests)
        if started_requests == 5:
            five_started.set()
        await release_requests.wait()
        active_requests -= 1
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(client=client, request_interval_seconds=0)
        tasks = [
            asyncio.create_task(fetcher.fetch(f"https://host-{index}.example/item"))
            for index in range(6)
        ]
        await asyncio.wait_for(five_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert started_requests == 5
        release_requests.set()
        await asyncio.gather(*tasks)

    assert maximum_active_requests == 5
