"""Shared collection-domain values and extension interfaces."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


def _empty_object_mapping() -> dict[str, object]:
    return {}


@dataclass(frozen=True, slots=True)
class CollectedItem:
    """One normalized item returned by a collector before persistence."""

    title: str
    original_url: str
    canonical_url: str
    published_at: datetime | None = None
    summary: str | None = None
    extra: Mapping[str, object] = field(default_factory=_empty_object_mapping)


@dataclass(frozen=True, slots=True)
class CollectContext:
    """Runtime source information supplied to a collector."""

    source_url: str
    source_name: str | None = None
    config: Mapping[str, object] = field(default_factory=_empty_object_mapping)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Transport-neutral HTTP response data used by collectors."""

    requested_url: str
    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    encoding: str = "utf-8"

    @property
    def text(self) -> str:
        """Decode the body, replacing malformed byte sequences."""

        try:
            return self.content.decode(self.encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class Fetcher(Protocol):
    """Fetch an HTTP(S) resource without exposing a concrete client."""

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult: ...


class Collector(Protocol):
    """Collect normalized list/feed entries for one source."""

    @property
    def name(self) -> str: ...

    async def collect(self, context: CollectContext) -> list[CollectedItem]: ...
