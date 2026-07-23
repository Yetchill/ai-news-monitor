"""Document/case hub collector interfaces with a small fixture-ready implementation."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import cast

from bs4 import BeautifulSoup, Tag

from app.collectors.html_list import HTMLListCollector
from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.utils.dates import parse_datetime
from app.utils.fingerprint import generate_item_fingerprint
from app.utils.url import resolve_url


class HubCollector(ABC):
    """Stable interface for future document/case-specific hub adapters."""

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    @abstractmethod
    async def collect(self, context: CollectContext) -> list[CollectedItem]: ...


class DocumentHubCollector(HubCollector):
    """Extract report parents and explicitly nested case children from public HTML."""

    name = "document_hub"

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        response = await self._fetcher.fetch(context.source_url)
        soup = BeautifulSoup(response.content, "lxml")
        config = _mapping(context.config.get("extraction"))
        report_selector = _required(config, "report_item_selector")
        case_selector = _required(config, "case_item_selector")
        max_items = min(100, max(1, _integer(context.config.get("max_items"), 20)))
        items: list[CollectedItem] = []
        for report in soup.select(report_selector):
            if len(items) >= max_items:
                break
            parent = _node_item(report, response.url, config, prefix="report")
            if parent is None:
                continue
            parent_fingerprint = generate_item_fingerprint(parent.title)
            items.append(parent)
            for case in report.select(case_selector):
                if len(items) >= max_items:
                    break
                child = _node_item(case, response.url, config, prefix="case")
                if child is None:
                    continue
                items.append(
                    CollectedItem(
                        title=child.title,
                        original_url=child.original_url,
                        canonical_url=child.canonical_url,
                        published_at=child.published_at,
                        summary=child.summary,
                        extra={
                            **dict(child.extra),
                            "record_kind": "case",
                            "parent_fingerprint": parent_fingerprint,
                        },
                    )
                )
        return items


class CaseHubCollector(HubCollector):
    """Case-hub interface using validated HTML-list configuration as its base adapter."""

    name = "case_hub"

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        items = await HTMLListCollector(self._fetcher).collect(context)
        return [
            CollectedItem(
                title=item.title,
                original_url=item.original_url,
                canonical_url=item.canonical_url,
                published_at=item.published_at,
                summary=item.summary,
                extra={**dict(item.extra), "record_kind": "case"},
            )
            for item in items
        ]


def _node_item(
    node: Tag, page_url: str, config: Mapping[str, object], *, prefix: str
) -> CollectedItem | None:
    title_selector = _required(config, f"{prefix}_title_selector")
    link_selector = _required(config, f"{prefix}_link_selector")
    title_node = node.select_one(title_selector)
    link_node = node.select_one(link_selector)
    if title_node is None or link_node is None:
        return None
    title = " ".join(title_node.get_text(" ", strip=True).split())
    href = link_node.get("href")
    if len(title) < 4 or not isinstance(href, str):
        return None
    url = resolve_url(page_url, href)
    if url is None:
        return None
    date_selector = _text(config.get(f"{prefix}_date_selector"))
    summary_selector = _text(config.get(f"{prefix}_summary_selector"))
    date_node = node.select_one(date_selector) if date_selector else None
    summary_node = node.select_one(summary_selector) if summary_selector else None
    return CollectedItem(
        title=title,
        original_url=url,
        canonical_url=url,
        published_at=parse_datetime(date_node.get_text(" ", strip=True) if date_node else None),
        summary=(
            " ".join(summary_node.get_text(" ", strip=True).split()) if summary_node else None
        ),
        extra={"record_kind": "report" if prefix == "report" else "case", "detail_fetched": True},
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): child for key, child in cast(Mapping[object, object], value).items()}


def _required(value: Mapping[str, object], key: str) -> str:
    result = _text(value.get(key))
    if result is None:
        raise ValueError(f"document hub requires extraction.{key}")
    return result


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
