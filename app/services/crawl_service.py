"""Network collection coordination without database transaction ownership."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

from app.collectors.registry import CollectorRegistry
from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.domain.enums import SourceOrigin, SourceType
from app.domain.models import Source
from app.domain.update import UpdateMode


class CrawlService:
    """Create collectors from source configuration and execute bounded collection."""

    def __init__(
        self,
        registry: CollectorRegistry,
        fetcher: Fetcher,
        *,
        user_source_fetcher: Fetcher | None = None,
    ) -> None:
        self._registry = registry
        self._fetcher = fetcher
        self._user_source_fetcher = user_source_fetcher

    async def collect(
        self,
        source: Source,
        *,
        mode: UpdateMode = UpdateMode.INCREMENTAL,
        max_pages: int | None = None,
        max_items: int | None = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
    ) -> list[CollectedItem]:
        config = _runtime_config(
            source,
            mode=mode,
            max_pages=max_pages,
            max_items=max_items,
            published_from=published_from,
            published_to=published_to,
        )
        fetcher = (
            self._user_source_fetcher
            if source.origin is SourceOrigin.USER_ADDED and self._user_source_fetcher is not None
            else self._fetcher
        )
        collector = self._registry.create(source, fetcher)
        items = await collector.collect(
            CollectContext(
                source_url=source.start_url,
                source_name=source.name,
                config=config,
            )
        )
        start = _utc(published_from)
        end = _utc(published_to)
        if start is None and end is None:
            return items
        return [item for item in items if _within_range(item.published_at, start, end)]


def _runtime_config(
    source: Source,
    *,
    mode: UpdateMode,
    max_pages: int | None,
    max_items: int | None,
    published_from: datetime | None,
    published_to: datetime | None,
) -> dict[str, object]:
    config: dict[str, Any] = deepcopy(source.collector_config)
    config["update_mode"] = mode.value
    if max_items is not None:
        value = max(1, max_items)
        config["max_items"] = value
        if source.source_type is SourceType.GITHUB_RELEASE:
            config["max_releases"] = value
        discovery = _child_mapping(config, "discovery")
        discovery["max_items"] = value
    if max_pages is not None:
        _child_mapping(config, "discovery")["max_pages"] = max(1, max_pages)
    if published_from is not None:
        normalized_from = _utc(published_from)
        assert normalized_from is not None
        config["published_from"] = normalized_from.isoformat()
    if published_to is not None:
        normalized_to = _utc(published_to)
        assert normalized_to is not None
        config["published_to"] = normalized_to.isoformat()
    return cast(dict[str, object], config)


def _child_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return cast(dict[str, Any], value)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _within_range(
    value: datetime | None,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    if value is None:
        return True
    normalized = _utc(value)
    assert normalized is not None
    return (start is None or normalized >= start) and (end is None or normalized <= end)
