# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Stage-five-B source discovery, SSRF, preview, token, and management tests."""

import gzip
import ssl
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.domain.collection import FetchResult
from app.domain.enums import Category, CrawlStatus, DiscoveryStatus, SourceOrigin, SourceType
from app.domain.models import Source
from app.domain.onboarding import DiscoveryResult, DiscoverySession, PreviewItem, PreviewResult
from app.domain.update import UpdateResult
from app.fetchers.errors import FetchError, FetchTimeoutError
from app.services.crawl_service import CrawlService
from app.services.source_discovery import (
    DiscoveryTokenError,
    DiscoveryTokenStore,
    SourceDiscoveryService,
    SourcePreviewService,
)
from app.services.source_management import (
    SourceAlreadyExistsError,
    SourceManagementError,
    SourceManagementService,
    SourceOnboardingService,
)
from app.services.source_url_security import (
    PublicAddressNetworkBackend,
    ResponseTooLargeFetchError,
    SafeHttpFetcher,
    SourceUrlGuard,
    SourceUrlSecurityError,
)
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures"
PUBLIC_IP = "93.184.216.34"


async def public_resolver(_hostname: str) -> Sequence[str]:
    return (PUBLIC_IP,)


class FakeFetcher:
    def __init__(self, results: Mapping[str, FetchResult | Exception]) -> None:
        self.results = dict(results)
        self.calls: list[str] = []

    async def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> FetchResult:
        del headers
        self.calls.append(url)
        result = self.results.get(url)
        if result is None:
            raise FetchError(url, "not configured")
        if isinstance(result, Exception):
            raise result
        return result

    async def post(
        self,
        url: str,
        *,
        body: str,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """POST lookup using the same results mapping with key f'{url}|{body}'."""
        del headers
        key = f"{url}|{body}"
        self.calls.append(key)
        result = self.results.get(key)
        if result is None:
            raise FetchError(key, "not configured")
        if isinstance(result, Exception):
            raise result
        return result


class DummyNetworkStream(httpcore.AsyncNetworkStream):
    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del buffer, timeout

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del ssl_context, server_hostname, timeout
        return self


class RecordingNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del port, timeout, local_address, socket_options
        self.hosts.append(host)
        return DummyNetworkStream()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise AssertionError("Unix socket must not be used")

    async def sleep(self, seconds: float) -> None:
        del seconds


def response(url: str, content: bytes, content_type: str) -> FetchResult:
    return FetchResult(
        requested_url=url,
        url=url,
        status_code=200,
        headers={"content-type": content_type},
        content=content,
    )


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def discovery_service(results: Mapping[str, FetchResult | Exception]) -> SourceDiscoveryService:
    return SourceDiscoveryService(FakeFetcher(results), SourceUrlGuard(public_resolver))


@pytest.mark.asyncio
async def test_discovers_direct_rss_url() -> None:
    url = "https://example.com/feed.xml"
    result = await discovery_service(
        {url: response(url, fixture("sample_rss.xml"), "application/rss+xml")}
    ).discover(url)

    assert result.source_type is SourceType.RSS
    assert result.collector_name == "rss"
    assert result.discovery_status is DiscoveryStatus.READY
    assert result.discovery_confidence >= 0.9


@pytest.mark.asyncio
async def test_feed_with_mostly_invalid_entries_is_not_reported_ready() -> None:
    url = "https://example.com/feed.xml"
    invalid = "".join(f"<item><title>无链接 {index}</title></item>" for index in range(9))
    content = (
        f"<rss version='2.0'><channel><title>Feed</title>{invalid}"
        "<item><title>唯一有效</title><link>https://example.com/valid</link></item>"
        "</channel></rss>"
    ).encode()

    result = await discovery_service({url: response(url, content, "application/rss+xml")}).discover(
        url
    )

    assert result.requires_custom_collector is True


@pytest.mark.asyncio
async def test_discovers_html_alternate_feed() -> None:
    page = "https://example.com/"
    feed = "https://example.com/news.atom"
    html = (
        b'<html><head><link rel="alternate" type="application/atom+xml" '
        b'href="/news.atom"></head></html>'
    )
    result = await discovery_service(
        {
            page: response(page, html, "text/html"),
            feed: response(feed, fixture("sample_atom.xml"), "application/atom+xml"),
        }
    ).discover(page)

    assert result.source_type is SourceType.RSS
    assert result.normalized_url == feed


@pytest.mark.asyncio
async def test_private_alternate_feed_is_never_fetched() -> None:
    page = "https://example.com/"
    private_feed = "https://private.example/feed.xml"
    html = (
        b'<html><head><link rel="alternate" type="application/rss+xml" '
        b'href="https://private.example/feed.xml"></head></html>'
    )

    async def resolver(hostname: str) -> Sequence[str]:
        return ("10.0.0.8",) if hostname == "private.example" else (PUBLIC_IP,)

    fetcher = FakeFetcher({page: response(page, html, "text/html")})
    result = await SourceDiscoveryService(fetcher, SourceUrlGuard(resolver)).discover(page)

    assert result.requires_custom_collector is True
    assert private_feed not in fetcher.calls


@pytest.mark.asyncio
async def test_common_feed_paths_are_revalidated_after_dns_change() -> None:
    page = "https://example.com/"
    calls = 0

    async def resolver(_hostname: str) -> Sequence[str]:
        nonlocal calls
        calls += 1
        return (PUBLIC_IP,) if calls == 1 else ("10.0.0.8",)

    fetcher = FakeFetcher({page: response(page, b"<html></html>", "text/html")})
    result = await SourceDiscoveryService(fetcher, SourceUrlGuard(resolver)).discover(page)

    assert result.requires_custom_collector is True
    assert fetcher.calls == [page]


@pytest.mark.asyncio
async def test_one_onboarding_attempt_has_fixed_overall_request_bound() -> None:
    page = "https://example.com/"
    alternate_urls = [f"https://feeds.example/{index}.xml" for index in range(4)]
    common_urls = [
        "https://example.com/feed",
        "https://example.com/rss",
        "https://example.com/atom.xml",
        "https://example.com/feed.xml",
    ]
    links = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="{url}">' for url in alternate_urls
    )
    items = "".join(
        f"<li><a href='/news/{index}'>有效新闻标题 {index}</a></li>" for index in range(4)
    )
    html = (
        f"<html><head>{links}</head><body><ul class='news-list'>{items}</ul></body></html>".encode()
    )
    not_feed = b"<html><body>not a feed</body></html>"
    fetcher = FakeFetcher(
        {
            page: response(page, html, "text/html"),
            **{url: response(url, not_feed, "text/html") for url in alternate_urls + common_urls},
        }
    )
    guard = SourceUrlGuard(public_resolver)
    store = DiscoveryTokenStore()
    service = SourceOnboardingService(
        SourceDiscoveryService(fetcher, guard),
        SourcePreviewService(
            default_collector_registry(), fetcher, RuleBasedClassifier.from_yaml()
        ),
        store,
    )

    token = await service.start(page)

    assert store.get(token).preview.items
    assert len(fetcher.calls) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/QwenLM/Qwen-Agent",
        "https://github.com/QwenLM/Qwen-Agent/releases",
    ],
)
async def test_discovers_github_repository_and_releases(url: str) -> None:
    result = await discovery_service({}).discover(url)

    assert result.source_type is SourceType.GITHUB_RELEASE
    assert result.collector_name == "github_release"
    assert result.normalized_url == "https://github.com/QwenLM/Qwen-Agent/releases"


@pytest.mark.asyncio
async def test_rejects_non_repository_github_page() -> None:
    result = await discovery_service({}).discover("https://github.com/search")

    assert result.requires_custom_collector is True
    assert result.discovery_status is DiscoveryStatus.NEEDS_CUSTOM_COLLECTOR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example",
        "https://github.com/orgs/example",
        "https://github.com/example/project/issues",
        "https://github.com/example/project/pulls",
        "https://github.com/example/project/releases/latest",
        "https://github.com/example/project/releases?tab=readme",
        "https://github.com/example%2Fproject/releases",
        "https://github.com/example//project",
        "https://github.com/-invalid/project",
        "https://github.com/example/.git",
    ],
)
async def test_rejects_github_pages_outside_strict_repository_shapes(url: str) -> None:
    result = await discovery_service({}).discover(url)

    assert result.requires_custom_collector is True
    assert result.collector_name == "custom"


@pytest.mark.asyncio
async def test_discovers_common_html_list_with_auditable_fixed_selector() -> None:
    url = "https://example.com/news"
    result = await discovery_service(
        {url: response(url, fixture("sample_list.html"), "text/html")}
    ).discover(url)

    assert result.source_type is SourceType.HTML_LIST
    assert result.collector_config["extraction"]["item_selector"] == ".news-list li"
    assert result.requires_custom_collector is False


@pytest.mark.asyncio
async def test_unreliable_html_requires_custom_collector() -> None:
    url = "https://example.com/"
    html = b"<html><body><a href='/about'>About us</a></body></html>"
    result = await discovery_service({url: response(url, html, "text/html")}).discover(url)

    assert result.requires_custom_collector is True
    assert result.collector_name == "custom"


@pytest.mark.asyncio
async def test_navigation_and_footer_links_do_not_become_html_source() -> None:
    url = "https://example.com/"
    html = b"""
    <html><body>
      <nav><h3><a href='/news/login'>Login account</a></h3></nav>
      <footer>
        <h3><a href='/news/privacy'>Privacy policy</a></h3>
        <h3><a href='/news/contact'>Contact information</a></h3>
        <h3><a href='/news/links'>Friendly links</a></h3>
      </footer>
    </body></html>
    """

    result = await discovery_service({url: response(url, html, "text/html")}).discover(url)

    assert result.requires_custom_collector is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "data:text/plain,secret",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://169.254.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:password@example.com/",
        "https://example.com:8443/",
    ],
)
async def test_url_guard_rejects_unsafe_inputs(url: str) -> None:
    guard = SourceUrlGuard(public_resolver)
    with pytest.raises(SourceUrlSecurityError):
        await guard.validate(url)


@pytest.mark.asyncio
async def test_url_guard_rejects_dns_resolving_to_non_public_addresses() -> None:
    async def private_resolver(_hostname: str) -> Sequence[str]:
        return ("10.0.0.8", "93.184.216.34")

    with pytest.raises(SourceUrlSecurityError):
        await SourceUrlGuard(private_resolver).validate("https://example.com/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://0177.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fc00::1]/",
        "http://[2001:db8::1]/",
        "http://255.255.255.255/",
    ],
)
async def test_url_guard_rejects_mapped_reserved_and_ambiguous_ip_forms(url: str) -> None:
    with pytest.raises(SourceUrlSecurityError):
        await SourceUrlGuard(public_resolver).validate(url)


@pytest.mark.asyncio
async def test_url_guard_normalizes_idn_case_trailing_dot_and_ipv6_brackets() -> None:
    guard = SourceUrlGuard(public_resolver)

    assert await guard.validate("HTTPS://BÜCHER.Example.:443/feed/#part") == (
        "https://xn--bcher-kva.example/feed"
    )
    assert await guard.validate("https://[2606:4700:4700::1111]:443/feed/") == (
        "https://[2606:4700:4700::1111]/feed"
    )


@pytest.mark.asyncio
async def test_connect_backend_dials_validated_ip_literal_not_original_hostname() -> None:
    recording = RecordingNetworkBackend()
    backend = PublicAddressNetworkBackend(SourceUrlGuard(public_resolver), recording)

    await backend.connect_tcp("example.com", 443, timeout=1)

    assert recording.hosts == [PUBLIC_IP]


@pytest.mark.asyncio
async def test_safe_fetcher_rechecks_dns_at_connect_and_blocks_rebinding() -> None:
    calls = 0

    async def rebinding_resolver(_hostname: str) -> Sequence[str]:
        nonlocal calls
        calls += 1
        return (PUBLIC_IP,) if calls == 1 else ("127.0.0.1",)

    recording = RecordingNetworkBackend()
    async with SafeHttpFetcher(
        SourceUrlGuard(rebinding_resolver), network_backend=recording
    ) as fetcher:
        with pytest.raises(SourceUrlSecurityError):
            await fetcher.fetch("https://example.com/")

    assert calls == 2
    assert recording.hosts == []


def test_url_guard_rejects_overlong_url() -> None:
    with pytest.raises(SourceUrlSecurityError):
        SourceUrlGuard(public_resolver).normalize("https://example.com/" + "x" * 2048)


@pytest.mark.asyncio
async def test_safe_fetcher_revalidates_redirect_and_rejects_private_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1/private"}, request=request
        )

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(handler)
    ) as fetcher:
        with pytest.raises(SourceUrlSecurityError):
            await fetcher.fetch("https://example.com/start")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/private",
        "https://example.com:8443/private",
        "https://user%40name:password@example.com/private",
    ],
)
async def test_safe_fetcher_rejects_unsafe_redirect_forms(location: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"location": location}, request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(handler)
    ) as fetcher:
        with pytest.raises(SourceUrlSecurityError):
            await fetcher.fetch("https://example.com/start")

    assert requests == 1


@pytest.mark.asyncio
async def test_safe_fetcher_blocks_https_downgrade_and_redirect_loops() -> None:
    def downgrade(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "http://example.com/plain"}, request=request
        )

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(downgrade)
    ) as fetcher:
        with pytest.raises(SourceUrlSecurityError, match="降级"):
            await fetcher.fetch("https://example.com/start")

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"}, request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver),
        transport=httpx.MockTransport(loop),
        max_redirects=2,
    ) as fetcher:
        with pytest.raises(FetchError, match="重定向"):
            await fetcher.fetch("https://example.com/start")


@pytest.mark.asyncio
async def test_safe_fetcher_drops_sensitive_headers_and_cookies_across_hosts() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "https://other.example/final",
                    "set-cookie": "session=remote-secret",
                },
                request=request,
            )
        return httpx.Response(200, content=b"ok", request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(handler)
    ) as fetcher:
        await fetcher.fetch(
            "https://example.com/start",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "local=secret",
                "Host": "internal.example",
                "Accept": "application/xml",
            },
        )

    assert len(seen) == 2
    assert all("authorization" not in request.headers for request in seen)
    assert all("cookie" not in request.headers for request in seen)
    assert [request.headers["host"] for request in seen] == ["example.com", "other.example"]
    assert all(request.headers["accept"] == "application/xml" for request in seen)


@pytest.mark.asyncio
async def test_safe_fetcher_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"direct", request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(handler)
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/direct")

    assert result.content == b"direct"
    assert seen == ["https://example.com/direct"]


@pytest.mark.asyncio
async def test_safe_fetcher_preserves_safe_redirect_path_spelling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed":
            return httpx.Response(308, headers={"location": "/feed/"}, request=request)
        return httpx.Response(200, content=b"feed", request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(handler)
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/feed/")

    assert result.content == b"feed"
    assert result.url == "https://example.com/feed/"


@pytest.mark.asyncio
async def test_safe_fetcher_post_does_not_follow_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "http://evil.internal/leak"}, request=request
        )

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(handler)
    ) as fetcher:
        result = await fetcher.post(
            "https://example.com/api",
            body='{"key":"value"}',
        )

    assert result.status_code == 302
    assert result.headers.get("location") == "http://evil.internal/leak"


@pytest.mark.asyncio
async def test_safe_fetcher_post_rejects_invalid_url() -> None:
    def ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(ok)
    ) as fetcher:
        with pytest.raises(SourceUrlSecurityError):
            await fetcher.post(
                "http://127.0.0.1/internal",
                body="{}",
            )


@pytest.mark.asyncio
async def test_safe_fetcher_enforces_response_size_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2000, request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver),
        transport=httpx.MockTransport(handler),
        max_response_bytes=1024,
    ) as fetcher:
        with pytest.raises(ResponseTooLargeFetchError):
            await fetcher.fetch("https://example.com/large")


@pytest.mark.asyncio
async def test_safe_fetcher_limits_chunked_and_decompressed_response_bytes() -> None:
    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield b"x" * 700
            yield b"x" * 700

    def chunked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedStream(), request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver),
        transport=httpx.MockTransport(chunked),
        max_response_bytes=1024,
    ) as fetcher:
        with pytest.raises(ResponseTooLargeFetchError):
            await fetcher.fetch("https://example.com/chunked")

    compressed = gzip.compress(b"z" * 5000)

    def gzip_bomb(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=compressed,
            request=request,
        )

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver),
        transport=httpx.MockTransport(gzip_bomb),
        max_response_bytes=1024,
    ) as fetcher:
        with pytest.raises(ResponseTooLargeFetchError):
            await fetcher.fetch("https://example.com/compressed")


@pytest.mark.asyncio
async def test_safe_fetcher_reports_timeout_without_response_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("internal path /private/project", request=request)

    async with SafeHttpFetcher(
        SourceUrlGuard(public_resolver), transport=httpx.MockTransport(handler)
    ) as fetcher:
        with pytest.raises(FetchTimeoutError, match="网页响应超时"):
            await fetcher.fetch("https://example.com/slow")


@pytest.mark.asyncio
async def test_preview_reuses_registry_and_classifier_caps_at_ten_without_database(
    database: Database,
) -> None:
    url = "https://example.com/feed.xml"
    entries = "".join(
        f"<item><title>行业动态标题 {index}</title><link>https://example.com/{index}</link></item>"
        for index in range(15)
    )
    rss = f"<rss version='2.0'><channel><title>Feed</title>{entries}</channel></rss>".encode()
    fetcher = FakeFetcher({url: response(url, rss, "application/rss+xml")})
    preview = SourcePreviewService(
        default_collector_registry(), fetcher, RuleBasedClassifier.from_yaml()
    )
    result = await preview.preview(_discovery(url))

    assert len(result.items) == 10
    assert all(isinstance(item.category, Category) for item in result.items)
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.list() == []
        assert uow.crawl_runs.list() == []


@pytest.mark.asyncio
async def test_formal_user_added_source_uses_safe_fetcher_route() -> None:
    url = "https://example.com/feed.xml"
    trusted_fetcher = FakeFetcher({})
    user_fetcher = FakeFetcher(
        {url: response(url, fixture("sample_rss.xml"), "application/rss+xml")}
    )
    source = Source(
        name="用户来源",
        source_type=SourceType.RSS,
        start_url=url,
        enabled=True,
        collector_name="rss",
        collector_config={"max_items": 10},
        origin=SourceOrigin.USER_ADDED,
    )

    items = await CrawlService(
        default_collector_registry(),
        trusted_fetcher,
        user_source_fetcher=user_fetcher,
    ).collect(source)

    assert items
    assert trusted_fetcher.calls == []
    assert user_fetcher.calls == [url]


@pytest.mark.asyncio
async def test_low_quality_duplicate_preview_cannot_enable_and_state_is_bounded() -> None:
    url = "https://example.com/feed.xml"
    entries = "".join(
        f"<item><title>{'重复标题' + '很长' * 600}</title>"
        f"<link>https://example.com/{index}</link><description>{'摘要' * 1500}</description></item>"
        for index in range(10)
    )
    rss = f"<rss version='2.0'><channel><title>Feed</title>{entries}</channel></rss>".encode()
    preview = SourcePreviewService(
        default_collector_registry(),
        FakeFetcher({url: response(url, rss, "application/rss+xml")}),
        RuleBasedClassifier.from_yaml(),
    )

    result = await preview.preview(_discovery(url))

    assert result.can_enable is False
    assert any("重复或无效记录过多" in error for error in result.errors)


def test_token_store_rejects_forgery_expiry_and_evicts_oldest() -> None:
    session = DiscoverySession(_discovery("https://example.com/feed"), PreviewResult(()))
    store = DiscoveryTokenStore(ttl_seconds=0.01, max_entries=2)
    first = store.put(session)
    second = store.put(session)
    third = store.put(session)
    assert len(store) == 2
    with pytest.raises(DiscoveryTokenError):
        store.get(first)
    assert store.get(second) is session
    assert store.get(third) is session
    with pytest.raises(DiscoveryTokenError):
        store.get("forged")
    sleep(0.02)
    with pytest.raises(DiscoveryTokenError):
        store.get(second)


def test_token_store_claim_is_exclusive_and_concurrent_capacity_is_bounded() -> None:
    session = DiscoverySession(_discovery("https://example.com/feed"), PreviewResult(()))
    store = DiscoveryTokenStore(max_entries=32)
    token = store.put(session)

    def claim() -> DiscoverySession | None:
        try:
            return store.claim(token)
        except DiscoveryTokenError:
            return None

    def claim_once(_index: int) -> DiscoverySession | None:
        return claim()

    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(executor.map(claim_once, range(8)))

    assert sum(result is session for result in claimed) == 1
    store.discard(token)

    def fill(worker: int) -> tuple[str, ...]:
        return tuple(store.put(session) for _index in range(worker, worker + 80))

    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = [token for group in executor.map(fill, range(8)) for token in group]

    assert len(tokens) == len(set(tokens))
    assert len(store) == 32


def test_save_source_uses_server_state_rejects_duplicate_and_does_not_create_business_data(
    database: Database,
) -> None:
    store = DiscoveryTokenStore()
    session = DiscoverySession(
        _discovery("https://example.com/feed"),
        PreviewResult(
            (PreviewItem("标题", "https://example.com/1", None, None, Category.UNCLASSIFIED),)
        ),
    )
    token = store.put(session)
    service = SourceManagementService(lambda: RepositoryUnitOfWork(database), store)
    saved = service.create_from_token(
        token,
        name="用户来源",
        default_category="policy_industry",
        enabled=True,
        description="只保存服务器检测配置",
    )

    assert saved.enabled is False
    assert saved.origin is SourceOrigin.USER_ADDED
    with pytest.raises(DiscoveryTokenError):
        service.create_from_token(
            token,
            name="重复使用 token",
            default_category=None,
            enabled=False,
            description=None,
        )
    with RepositoryUnitOfWork(database) as uow:
        source = uow.sources.get(saved.id)
        assert source is not None
        assert source.collector_name == "rss"
        assert source.collector_config == {"max_items": 1000}
        assert source.description == "只保存服务器检测配置"
        assert uow.items.list() == []
        assert uow.crawl_runs.list() == []

    duplicate_token = store.put(session)
    with pytest.raises(SourceAlreadyExistsError):
        service.create_from_token(
            duplicate_token,
            name="重复",
            default_category=None,
            enabled=False,
            description=None,
        )


def test_unrecognized_or_empty_preview_source_is_saved_as_blocked_candidate(
    database: Database,
) -> None:
    store = DiscoveryTokenStore()
    unusable = DiscoverySession(
        DiscoveryResult(
            collector_name="custom",
            source_type=SourceType.CUSTOM,
            normalized_url="https://example.com/complex",
            discovery_status=DiscoveryStatus.NEEDS_CUSTOM_COLLECTOR,
            discovery_confidence=0,
            requires_custom_collector=True,
            collector_config={},
            explanations=("需要适配",),
            errors=("需要适配",),
            tested_at=datetime.now(UTC),
        ),
        PreviewResult(()),
    )
    service = SourceManagementService(lambda: RepositoryUnitOfWork(database), store)
    saved = service.create_from_token(
        store.put(unusable),
        name="复杂来源",
        default_category=None,
        enabled=True,
        description=None,
    )
    assert saved.enabled is False
    assert saved.requires_custom_collector is True


def test_concurrent_equivalent_url_save_returns_existing_source(database: Database) -> None:
    store = DiscoveryTokenStore()
    service = SourceManagementService(lambda: RepositoryUnitOfWork(database), store)
    preview = PreviewResult(
        (PreviewItem("标题", "https://example.com/item", None, None, Category.UNCLASSIFIED),)
    )
    normalized = "https://example.com/feed?a=1&b=2"
    tokens = [
        store.put(DiscoverySession(_discovery(normalized), preview)),
        store.put(DiscoverySession(_discovery(normalized), preview)),
    ]

    def save(index: int) -> int:
        try:
            return service.create_from_token(
                tokens[index],
                name=f"并发来源 {index}",
                default_category=None,
                enabled=True,
                description=None,
            ).id
        except SourceAlreadyExistsError as exc:
            return exc.source_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        source_ids = list(executor.map(save, range(2)))

    assert source_ids[0] == source_ids[1]
    with RepositoryUnitOfWork(database) as uow:
        assert len(uow.sources.list()) == 1


def test_edit_only_changes_allowed_fields_and_confirmed_rediscovery_is_atomic(
    database: Database,
) -> None:
    store = DiscoveryTokenStore()
    service = SourceManagementService(lambda: RepositoryUnitOfWork(database), store)
    original_session = DiscoverySession(
        _discovery("https://example.com/old"),
        PreviewResult(
            (PreviewItem("标题", "https://example.com/1", None, None, Category.UNCLASSIFIED),)
        ),
    )
    source = service.create_from_token(
        store.put(original_session),
        name="原名称",
        default_category=None,
        enabled=True,
        description=None,
    )
    service.edit(
        source.id,
        name="新名称",
        default_category="award_case",
        enabled=False,
        description="新说明",
    )
    with RepositoryUnitOfWork(database) as uow:
        before = uow.sources.get(source.id)
        assert before is not None
        assert before.start_url == "https://example.com/old"
        assert before.collector_name == "rss"

    rediscovery = DiscoverySession(
        replace(
            _discovery("https://example.com/new"),
            collector_name="html_list",
            source_type=SourceType.HTML_LIST,
            collector_config={"discovery": {"mode": "link_filter"}},
        ),
        PreviewResult(
            (PreviewItem("新标题", "https://example.com/n", None, None, Category.UNCLASSIFIED),)
        ),
        rediscover_source_id=source.id,
    )
    token = store.put(rediscovery)
    with RepositoryUnitOfWork(database) as uow:
        assert uow.sources.get(source.id).collector_name == "rss"  # type: ignore[union-attr]
    service.confirm_rediscovery(source.id, token)
    with RepositoryUnitOfWork(database) as uow:
        after = uow.sources.get(source.id)
        assert after is not None
        assert after.collector_name == "html_list"
        assert after.start_url == "https://example.com/new"
        assert after.name == "新名称"
        assert uow.items.list() == []


def test_rediscovery_token_is_source_bound_and_failure_preserves_old_config(
    database: Database,
) -> None:
    store = DiscoveryTokenStore()
    service = SourceManagementService(lambda: RepositoryUnitOfWork(database), store)
    preview = PreviewResult(
        (PreviewItem("标题", "https://example.com/1", None, None, Category.UNCLASSIFIED),)
    )
    first = service.create_from_token(
        store.put(DiscoverySession(_discovery("https://example.com/first"), preview)),
        name="第一来源",
        default_category=None,
        enabled=True,
        description=None,
    )
    second = service.create_from_token(
        store.put(DiscoverySession(_discovery("https://example.com/second"), preview)),
        name="第二来源",
        default_category=None,
        enabled=True,
        description=None,
    )
    failed = DiscoverySession(
        replace(
            _discovery("https://example.com/replacement"),
            collector_name="custom",
            source_type=SourceType.CUSTOM,
            discovery_status=DiscoveryStatus.NEEDS_CUSTOM_COLLECTOR,
            discovery_confidence=0,
            requires_custom_collector=True,
            collector_config={},
        ),
        PreviewResult(()),
        rediscover_source_id=first.id,
    )
    token = store.put(failed)

    with pytest.raises(SourceManagementError, match="不匹配"):
        service.confirm_rediscovery(second.id, token)
    service.confirm_rediscovery(first.id, token)

    with RepositoryUnitOfWork(database) as uow:
        source = uow.sources.get(first.id)
        assert source is not None
        assert source.start_url == "https://example.com/first"
        assert source.collector_name == "rss"
        assert source.collector_config == {"max_items": 1000}
        assert source.discovery_status == "needs_custom_collector"
        assert uow.items.list() == []

    conflicting = DiscoverySession(
        replace(_discovery("https://example.com/second"), collector_name="html_list"),
        preview,
        rediscover_source_id=first.id,
    )
    with pytest.raises(SourceAlreadyExistsError):
        service.confirm_rediscovery(first.id, store.put(conflicting))
    with RepositoryUnitOfWork(database) as uow:
        preserved = uow.sources.get(first.id)
        assert preserved is not None
        assert preserved.start_url == "https://example.com/first"
        assert preserved.collector_name == "rss"
        assert preserved.collector_config == {"max_items": 1000}


def test_web_discovery_preview_escapes_content_and_does_not_write_before_save(
    database: Database,
) -> None:
    url = "https://example.com/feed"
    store = DiscoveryTokenStore()
    token = store.put(
        DiscoverySession(
            _discovery(url),
            PreviewResult(
                (
                    PreviewItem(
                        "<script>alert(1)</script>",
                        "https://example.com/item",
                        None,
                        "<img src=x onerror=alert(2)>",
                        Category.UNCLASSIFIED,
                    ),
                )
            ),
        )
    )
    application = create_app(
        database=database,
        enforce_migrations=False,
        source_fetcher=FakeFetcher({}),
        source_url_guard=SourceUrlGuard(public_resolver),
        token_store=store,
    )
    with TestClient(application, raise_server_exceptions=False) as client:
        preview_response = client.get(f"/sources/discover/{token}")

    assert preview_response.status_code == 200
    assert "检测结果" in preview_response.text
    assert "<script>" not in preview_response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in preview_response.text
    assert "&lt;img src=x onerror=alert(2)&gt;" in preview_response.text
    with RepositoryUnitOfWork(database) as uow:
        assert uow.sources.list() == []
        assert uow.items.list() == []
        assert uow.crawl_runs.list() == []


def test_web_save_ignores_forged_collector_fields_and_edit_restricts_technical_fields(
    database: Database,
) -> None:
    url = "https://example.com/feed"
    application = create_app(
        database=database,
        enforce_migrations=False,
        source_fetcher=FakeFetcher(
            {url: response(url, fixture("sample_rss.xml"), "application/rss+xml")}
        ),
        source_url_guard=SourceUrlGuard(public_resolver),
    )
    with TestClient(application, raise_server_exceptions=False) as client:
        preview_response = client.post("/sources/discover", data={"url": url})
        token = _hidden_token(preview_response.text)
        save_response = client.post(
            "/sources",
            data={
                "token": token,
                "name": "网页来源",
                "enabled": "true",
                "action": "save",
                "collector_name": "custom",
                "collector_config": '{"danger": true}',
            },
            follow_redirects=False,
        )
        assert save_response.status_code == 303
        source_id = int(save_response.headers["location"].split("/")[2].split("?")[0])
        edit_response = client.post(
            f"/sources/{source_id}/edit",
            data={
                "name": "编辑后来源",
                "description": "说明",
                "default_category": "enterprise_case",
                "start_url": "https://attacker.example/",
                "collector_name": "custom",
                "collector_config": "{}",
            },
            follow_redirects=False,
        )
        assert edit_response.status_code == 303
        assert client.get("/sources/999999").status_code == 404
        assert client.get(f"/sources/{source_id}/edit").status_code == 405
        assert client.get(f"/sources/{source_id}/rediscover").status_code == 405

    with RepositoryUnitOfWork(database) as uow:
        source = uow.sources.get(source_id)
        assert source is not None
        assert source.name == "编辑后来源"
        assert source.start_url == url
        assert source.collector_name == "rss"
        assert source.collector_config == {"max_items": 1000}
        assert source.enabled is False


class RecordingOnboardingPipeline(UpdatePipeline):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[int | None] = []

    async def update(self, *, source_id: int | None = None, **_kwargs: object) -> UpdateResult:
        self.calls.append(source_id)
        if self.fail:
            raise RuntimeError("network failed /private/path token=secret")
        now = datetime.now(UTC)
        return UpdateResult(
            crawl_run_id=1,
            status=CrawlStatus.SUCCESS,
            started_at=now,
            finished_at=now,
            source_total=1,
            source_success=1,
            source_failed=0,
            discovered_count=1,
            new_count=1,
            updated_count=0,
            skipped_count=0,
            unclassified_count=0,
            error_summary=None,
            source_results=(),
        )


@pytest.mark.parametrize("fail", [False, True])
def test_save_and_update_saves_first_and_uses_existing_update_service(
    database: Database, fail: bool
) -> None:
    url = "https://example.com/feed"
    pipeline = RecordingOnboardingPipeline(fail=fail)

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
        source_fetcher=FakeFetcher(
            {url: response(url, fixture("sample_rss.xml"), "application/rss+xml")}
        ),
        source_url_guard=SourceUrlGuard(public_resolver),
    )
    with TestClient(application, raise_server_exceptions=False) as client:
        preview_response = client.post("/sources/discover", data={"url": url})
        result = client.post(
            "/sources",
            data={
                "token": _hidden_token(preview_response.text),
                "name": "保存并更新",
                "enabled": "true",
                "action": "save_and_update",
            },
        )

    assert result.status_code == 200
    assert pipeline.calls == []
    with RepositoryUnitOfWork(database) as uow:
        sources = uow.sources.list()
        assert len(sources) == 1
        assert sources[0].name == "保存并更新"


def _hidden_token(html: str) -> str:
    marker = 'name="token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def _discovery(url: str) -> DiscoveryResult:
    return DiscoveryResult(
        collector_name="rss",
        source_type=SourceType.RSS,
        normalized_url=url,
        discovery_status=DiscoveryStatus.READY,
        discovery_confidence=0.98,
        requires_custom_collector=False,
        collector_config={"max_items": 1000},
        explanations=("RSS",),
        errors=(),
        tested_at=datetime.now(UTC),
    )
