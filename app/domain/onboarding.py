"""Persistence-neutral source discovery and preview results."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.enums import Category, DiscoveryStatus, SourceType


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    collector_name: str
    source_type: SourceType
    normalized_url: str
    discovery_status: DiscoveryStatus
    discovery_confidence: float
    requires_custom_collector: bool
    collector_config: dict[str, Any]
    explanations: tuple[str, ...]
    errors: tuple[str, ...]
    tested_at: datetime

    @property
    def usable(self) -> bool:
        return not self.requires_custom_collector and self.discovery_status in {
            DiscoveryStatus.READY,
            DiscoveryStatus.PARTIAL,
        }


@dataclass(frozen=True, slots=True)
class PreviewItem:
    title: str
    url: str
    published_at: datetime | None
    summary: str | None
    category: Category


@dataclass(frozen=True, slots=True)
class PreviewResult:
    items: tuple[PreviewItem, ...]
    errors: tuple[str, ...] = ()

    @property
    def can_enable(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True, slots=True)
class DiscoverySession:
    discovery: DiscoveryResult
    preview: PreviewResult
    rediscover_source_id: int | None = None

    @property
    def can_enable(self) -> bool:
        return self.discovery.usable and self.preview.can_enable
