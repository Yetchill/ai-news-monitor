"""Strict source-catalog loading and conservative, idempotent database synchronization."""

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from app.config.settings import PROJECT_ROOT
from app.domain.enums import (
    CrawlMode,
    ImplementationStatus,
    LifecycleState,
    PrimaryType,
    ReviewPolicy,
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceRole,
    SourceTier,
    SourceType,
)
from app.domain.models import Source
from app.storage.repositories import RepositoryUnitOfWork
from app.utils.url import canonicalize_url

CATALOG_PATH = PROJECT_ROOT / "app" / "config" / "source_catalog.yaml"
UnitOfWorkFactory = Callable[[], RepositoryUnitOfWork]
_ROOT_KEYS = {"version", "sources"}
_ENTRY_KEYS = {
    "slug",
    "name",
    "url",
    "lifecycle_state",
    "source_role",
    "source_tier",
    "audience",
    "crawl_mode",
    "review_policy",
    "allowed_primary_types",
    "homepage_visible",
    "export_visible",
    "lookback_days",
    "max_items_per_run",
    "implementation_status",
    "implementation_reason",
    "verified_at",
    "activation_evidence",
    "notes",
    "collector_name",
    "collector_config",
    "include_terms",
    "exclude_terms",
    "minimum_quality_score",
    "allow_external_links",
}
_RETIRED_URLS = {
    "https://openai.com/news/rss.xml",
    "https://blog.google/rss/",
    "https://blog.google/rss",
    "https://github.com/QwenLM/Qwen-Agent/releases",
    "https://cloud.baidu.com/case/index.html",
}
_LEGACY_ALIASES = {
    "nda-news": ("国家数据局政策发布", "https://www.nda.gov.cn/sjj/zwgk/zcfb/list/index_pc_1.html"),
    "cac-policy-regulations": (
        "国家网信办网信发布",
        "https://www.cac.gov.cn/wxzw/wxfb/A093702index_1.htm",
    ),
    "caict-aihub-docs": ("AIIA 人工智能产业发展联盟", "https://www.aiiaorg.cn/"),
}
_LEGACY_MANAGED_HASHES = {
    "nda-news": "ae4c58340e948ae3ab74cc8995149b25444e3a55347de7f3924248059599d030",
    "cac-policy-regulations": "0d8a1d027514281d1bf2a278ff155f36fb4ddee26acc9077b73351a361743b81",
    "isc-notices": "898e711e6773336c91ece94c9367aced106ec733e9eb1e7f966bceabaaf2b26c",
    "caict-aihub-docs": "7d752f0e1cb208e12a151800bf695cab605280fda1c03f234c66e369a5893754",
    "baidu-cloud-news": "decb812b2466fe420be52514fafe44dac645ff69f058f1cc7a39972d1bd78574",
}
_LEGACY_MANAGED_FIELDS = (
    "name",
    "description",
    "source_type",
    "start_url",
    "default_category",
    "collector_name",
    "collector_config",
    "origin",
    "source_kind",
    "source_tier",
    "audience",
    "homepage_visible",
    "export_visible",
    "content_scope",
    "include_terms",
    "exclude_terms",
    "minimum_quality_score",
    "accept_title_only",
    "allow_external_links",
    "allow_technical_updates",
    "requires_custom_collector",
)


class SourceCatalogError(ValueError):
    pass


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: _UniqueLoader, node: MappingNode, *, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                key_node, deep=deep
            ),
        )
        if key in result:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                value_node, deep=deep
            ),
        )
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True, slots=True)
class SourceCatalogEntry:
    slug: str
    name: str
    url: str
    lifecycle_state: LifecycleState
    source_role: SourceRole
    source_tier: SourceTier
    audience: SourceAudience
    crawl_mode: CrawlMode
    review_policy: ReviewPolicy
    allowed_primary_types: tuple[PrimaryType, ...]
    homepage_visible: bool
    export_visible: bool
    lookback_days: int
    max_items_per_run: int
    implementation_status: ImplementationStatus
    implementation_reason: str
    verified_at: datetime | None
    activation_evidence: str | None
    notes: str
    collector_name: str
    collector_config: Mapping[str, object]
    include_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    minimum_quality_score: float
    allow_external_links: bool

    @property
    def source_type(self) -> SourceType:
        return {
            CrawlMode.RSS: SourceType.RSS,
            CrawlMode.HTML_LIST: SourceType.HTML_LIST,
            CrawlMode.API: SourceType.JSON_API,
        }.get(self.crawl_mode, SourceType.CUSTOM)


@dataclass(frozen=True, slots=True)
class SourceCatalogSyncResult:
    total: int
    created: int
    updated: int
    existing: int
    conflicts: int
    deleted: int
    retired: int
    active: int
    candidate: int
    paused: int
    implementation_counts: Mapping[str, int]

    @property
    def promoted(self) -> int:
        return self.updated

    @property
    def expected(self) -> int:
        return self.total

    @property
    def initialized(self) -> int:
        return self.created + self.updated


class SourceCatalogService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def load(self, path: Path = CATALOG_PATH) -> tuple[SourceCatalogEntry, ...]:
        return load_source_catalog(path)

    def sync(
        self,
        path: Path = CATALOG_PATH,
        *,
        reconcile: bool = False,
    ) -> SourceCatalogSyncResult:
        entries = self.load(path)
        created = updated = existing = conflicts = deleted = retired = 0
        with self._uow_factory() as uow:
            sources = uow.sources.list()
            by_slug = {source.slug: source for source in sources if source.slug}
            by_url = {
                canonicalize_url(source.start_url) or source.start_url: source for source in sources
            }
            for entry in entries:
                current = by_slug.get(entry.slug) or by_url.get(entry.url)
                if current is None:
                    current = _legacy_alias_source(entry, sources)
                if current is None:
                    source = uow.sources.add(_new_source(entry))
                    by_slug[entry.slug] = source
                    by_url[source.start_url] = source
                    created += 1
                    continue
                current_slug = current.slug
                if (
                    current_slug is not None
                    and current_slug != entry.slug
                    and not current_slug.startswith("legacy-source-")
                ):
                    conflicts += 1
                    continue
                current_fingerprint = _source_fingerprint(current)
                target_fingerprint = _entry_fingerprint(entry)
                if current.catalog_managed:
                    if not reconcile and current.catalog_fingerprint != current_fingerprint:
                        conflicts += 1
                        continue
                    needs_state_upgrade = (
                        current.lifecycle_state is LifecycleState.CANDIDATE
                        and entry.lifecycle_state is LifecycleState.ACTIVE
                    )
                    needs_reconcile = reconcile and (
                        current.lifecycle_state is not entry.lifecycle_state
                        or current.enabled
                        is not (entry.lifecycle_state is LifecycleState.ACTIVE)
                    )
                    if (
                        current_fingerprint == target_fingerprint
                        and not needs_state_upgrade
                        and not needs_reconcile
                    ):
                        existing += 1
                        continue
                    _apply_entry(
                        current,
                        entry,
                        preserve_paused=not reconcile,
                        preserve_active=not reconcile,
                    )
                    updated += 1
                    continue
                reconcilable_legacy = (
                    reconcile
                    and current.origin is SourceOrigin.PRESET
                    and (
                        current.slug == entry.slug
                        or current.slug is None
                        or current.slug.startswith("legacy-source-")
                    )
                )
                if reconcilable_legacy:
                    _apply_entry(current, entry, preserve_paused=False, preserve_active=False)
                    updated += 1
                    continue
                if _safe_legacy_match(current, entry):
                    _apply_entry(current, entry, preserve_paused=True, preserve_active=False)
                    updated += 1
                    continue
                conflicts += 1

            if reconcile:
                catalog_slugs = {entry.slug for entry in entries}
                for source in uow.sources.list():
                    if source.slug in catalog_slugs or source.origin is not SourceOrigin.PRESET:
                        continue
                    has_history = bool(
                        uow.items.count_by_source(source.id)
                        or uow.crawl_source_executions.count_by_source(source.id)
                    )
                    if not has_history:
                        uow.sources.delete(source.id)
                        deleted += 1
                        continue
                    source.enabled = False
                    source.lifecycle_state = LifecycleState.PAUSED
                    source.source_kind = SourceKind.FALLBACK
                    source.homepage_visible = False
                    source.export_visible = False
                    source.catalog_managed = False
                    source.catalog_fingerprint = None
                    retired += 1

            states: Counter[str] = Counter()
            implementations: Counter[str] = Counter()
            catalog_slugs = {entry.slug for entry in entries}
            for source in uow.sources.list():
                if source.slug not in catalog_slugs:
                    continue
                states[source.lifecycle_state.value] += 1
                implementations[source.implementation_status.value] += 1
        return SourceCatalogSyncResult(
            total=len(entries),
            created=created,
            updated=updated,
            existing=existing,
            conflicts=conflicts,
            deleted=deleted,
            retired=retired,
            active=states[LifecycleState.ACTIVE.value],
            candidate=states[LifecycleState.CANDIDATE.value],
            paused=states[LifecycleState.PAUSED.value],
            implementation_counts=dict(implementations),
        )


def load_source_catalog(path: Path = CATALOG_PATH) -> tuple[SourceCatalogEntry, ...]:
    try:
        with path.open(encoding="utf-8") as stream:
            raw = _mapping(yaml.load(stream, Loader=_UniqueLoader), "catalog")
    except (OSError, yaml.YAMLError) as exc:
        raise SourceCatalogError(f"cannot load source catalog {path}: {exc}") from exc
    _exact_keys(raw, _ROOT_KEYS, "catalog")
    if raw["version"] != 1:
        raise SourceCatalogError("catalog.version must be 1")
    raw_entries = _sequence(raw["sources"], "sources")
    entries: list[SourceCatalogEntry] = []
    slugs: set[str] = set()
    urls: set[str] = set()
    for index, raw_entry in enumerate(raw_entries, start=1):
        mapping = _mapping(raw_entry, f"sources[{index}]")
        _exact_keys(mapping, _ENTRY_KEYS, f"sources[{index}]")
        entry = _entry(mapping, index)
        if entry.slug in slugs:
            raise SourceCatalogError(f"duplicate source slug: {entry.slug}")
        if entry.url in urls:
            raise SourceCatalogError(f"duplicate source URL: {entry.url}")
        if entry.url in _RETIRED_URLS:
            raise SourceCatalogError(f"retired source must not be in catalog: {entry.slug}")
        slugs.add(entry.slug)
        urls.add(entry.url)
        entries.append(entry)
    return tuple(entries)


def _entry(value: Mapping[str, object], index: int) -> SourceCatalogEntry:
    location = f"sources[{index}]"
    slug = _text(value["slug"], f"{location}.slug")
    if len(slug) > 100 or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in slug
    ):
        raise SourceCatalogError(f"{location}.slug must be lowercase kebab-case")
    url = canonicalize_url(_text(value["url"], f"{location}.url"))
    if url is None:
        raise SourceCatalogError(f"{location}.url must be HTTP(S)")
    lifecycle = LifecycleState(_text(value["lifecycle_state"], "lifecycle_state"))
    implementation = ImplementationStatus(
        _text(value["implementation_status"], "implementation_status")
    )
    reason = _text(value["implementation_reason"], "implementation_reason")
    verified_at = _datetime_or_none(value["verified_at"], f"{location}.verified_at")
    evidence = _optional_text(value["activation_evidence"], "activation_evidence")
    if lifecycle is LifecycleState.ACTIVE:
        if (
            implementation is not ImplementationStatus.READY
            or verified_at is None
            or evidence is None
        ):
            raise SourceCatalogError(
                f"{location}: active requires ready, verified_at and activation_evidence"
            )
    elif not reason:
        raise SourceCatalogError(f"{location}: candidate/paused requires an explicit reason")
    role = SourceRole(_text(value["source_role"], "source_role"))
    homepage = _bool(value["homepage_visible"], "homepage_visible")
    export = _bool(value["export_visible"], "export_visible")
    review_policy = ReviewPolicy(_text(value["review_policy"], "review_policy"))
    if role is SourceRole.MEDIA_DISCOVERY and (
        homepage or export or review_policy is not ReviewPolicy.ALWAYS_REVIEW
    ):
        raise SourceCatalogError(f"{location}: media_discovery must be hidden and always_review")
    config = _mapping(value["collector_config"], f"{location}.collector_config")
    return SourceCatalogEntry(
        slug=slug,
        name=_text(value["name"], f"{location}.name"),
        url=url,
        lifecycle_state=lifecycle,
        source_role=role,
        source_tier=SourceTier(_text(value["source_tier"], "source_tier")),
        audience=SourceAudience(_text(value["audience"], "audience")),
        crawl_mode=CrawlMode(_text(value["crawl_mode"], "crawl_mode")),
        review_policy=review_policy,
        allowed_primary_types=tuple(
            PrimaryType(item)
            for item in _text_list(value["allowed_primary_types"], "allowed_primary_types")
        ),
        homepage_visible=homepage,
        export_visible=export,
        lookback_days=_bounded_int(value["lookback_days"], "lookback_days", 0, 3650),
        max_items_per_run=_bounded_int(value["max_items_per_run"], "max_items_per_run", 1, 200),
        implementation_status=implementation,
        implementation_reason=reason,
        verified_at=verified_at,
        activation_evidence=evidence,
        notes=_text(value["notes"], "notes"),
        collector_name=_text(value["collector_name"], "collector_name"),
        collector_config={str(key): deepcopy(child) for key, child in config.items()},
        include_terms=_text_list(value["include_terms"], "include_terms"),
        exclude_terms=_text_list(value["exclude_terms"], "exclude_terms"),
        minimum_quality_score=_score(value["minimum_quality_score"]),
        allow_external_links=_bool(value["allow_external_links"], "allow_external_links"),
    )


def _new_source(entry: SourceCatalogEntry) -> Source:
    source = Source(
        slug=entry.slug,
        name=entry.name,
        description=entry.notes,
        source_type=entry.source_type,
        start_url=entry.url,
        enabled=entry.lifecycle_state is LifecycleState.ACTIVE,
        lifecycle_state=entry.lifecycle_state,
        source_role=entry.source_role,
        crawl_mode=entry.crawl_mode,
        review_policy=entry.review_policy,
        allowed_primary_types=[item.value for item in entry.allowed_primary_types],
        lookback_days=entry.lookback_days,
        max_items_per_run=entry.max_items_per_run,
        implementation_status=entry.implementation_status,
        implementation_reason=entry.implementation_reason,
        activation_evidence=entry.activation_evidence,
        verified_at=entry.verified_at,
        last_preview_at=entry.verified_at,
        preview_item_count=1 if entry.lifecycle_state is LifecycleState.ACTIVE else None,
        preview_result=(
            {"status": "documented_fixture_or_live_preview", "catalog_sync": True}
            if entry.lifecycle_state is LifecycleState.ACTIVE
            else None
        ),
        catalog_managed=True,
        default_category=None,
        collector_name=entry.collector_name,
        collector_config=deepcopy(dict(entry.collector_config)),
        origin=SourceOrigin.PRESET,
        source_kind=SourceKind.FORMAL,
        source_tier=entry.source_tier,
        audience=entry.audience,
        homepage_visible=entry.homepage_visible,
        export_visible=entry.export_visible,
        content_scope=[],
        include_terms=list(entry.include_terms),
        exclude_terms=list(entry.exclude_terms),
        minimum_quality_score=entry.minimum_quality_score,
        accept_title_only=True,
        allow_external_links=entry.allow_external_links,
        allow_technical_updates=False,
        requires_custom_collector=entry.implementation_status
        is ImplementationStatus.NEEDS_CUSTOM_COLLECTOR,
    )
    source.catalog_fingerprint = _entry_fingerprint(entry)
    return source


def _apply_entry(
    source: Source,
    entry: SourceCatalogEntry,
    *,
    preserve_paused: bool,
    preserve_active: bool,
) -> None:
    paused = preserve_paused and source.lifecycle_state is LifecycleState.PAUSED
    active_override = preserve_active and (
        source.lifecycle_state is LifecycleState.ACTIVE
        and entry.lifecycle_state is LifecycleState.CANDIDATE
    )
    replacement = _new_source(entry)
    for field in _MANAGED_FIELDS:
        setattr(source, field, deepcopy(getattr(replacement, field)))
    if paused:
        source.lifecycle_state = LifecycleState.PAUSED
        source.enabled = False
    elif active_override:
        source.lifecycle_state = LifecycleState.ACTIVE
        source.enabled = True
    source.catalog_fingerprint = _entry_fingerprint(entry)


_MANAGED_FIELDS = (
    "slug",
    "name",
    "description",
    "source_type",
    "start_url",
    "enabled",
    "lifecycle_state",
    "source_role",
    "crawl_mode",
    "review_policy",
    "allowed_primary_types",
    "lookback_days",
    "max_items_per_run",
    "implementation_status",
    "implementation_reason",
    "activation_evidence",
    "verified_at",
    "last_preview_at",
    "preview_item_count",
    "preview_result",
    "catalog_managed",
    "default_category",
    "collector_name",
    "collector_config",
    "origin",
    "source_kind",
    "source_tier",
    "audience",
    "homepage_visible",
    "export_visible",
    "content_scope",
    "include_terms",
    "exclude_terms",
    "minimum_quality_score",
    "accept_title_only",
    "allow_external_links",
    "allow_technical_updates",
    "requires_custom_collector",
)

_FINGERPRINT_FIELDS = tuple(
    field
    for field in _MANAGED_FIELDS
    if field
    not in {
        "enabled",
        "lifecycle_state",
        "last_preview_at",
        "preview_item_count",
        "preview_result",
    }
)


def _source_fingerprint(source: Source) -> str:
    payload = {field: _json_value(getattr(source, field)) for field in _FINGERPRINT_FIELDS}
    return _hash(payload)


def _entry_fingerprint(entry: SourceCatalogEntry) -> str:
    source = _new_source_without_fingerprint(entry)
    return _source_fingerprint(source)


def _new_source_without_fingerprint(entry: SourceCatalogEntry) -> Source:
    # Avoid recursion through _new_source -> _entry_fingerprint.
    source = Source()
    values: dict[str, Any] = {
        "slug": entry.slug,
        "name": entry.name,
        "description": entry.notes,
        "source_type": entry.source_type,
        "start_url": entry.url,
        "enabled": entry.lifecycle_state is LifecycleState.ACTIVE,
        "lifecycle_state": entry.lifecycle_state,
        "source_role": entry.source_role,
        "crawl_mode": entry.crawl_mode,
        "review_policy": entry.review_policy,
        "allowed_primary_types": [item.value for item in entry.allowed_primary_types],
        "lookback_days": entry.lookback_days,
        "max_items_per_run": entry.max_items_per_run,
        "implementation_status": entry.implementation_status,
        "implementation_reason": entry.implementation_reason,
        "activation_evidence": entry.activation_evidence,
        "verified_at": entry.verified_at,
        "last_preview_at": entry.verified_at,
        "preview_item_count": 1 if entry.lifecycle_state is LifecycleState.ACTIVE else None,
        "preview_result": {"status": "documented_fixture_or_live_preview", "catalog_sync": True}
        if entry.lifecycle_state is LifecycleState.ACTIVE
        else None,
        "catalog_managed": True,
        "default_category": None,
        "collector_name": entry.collector_name,
        "collector_config": deepcopy(dict(entry.collector_config)),
        "origin": SourceOrigin.PRESET,
        "source_kind": SourceKind.FORMAL,
        "source_tier": entry.source_tier,
        "audience": entry.audience,
        "homepage_visible": entry.homepage_visible,
        "export_visible": entry.export_visible,
        "content_scope": [],
        "include_terms": list(entry.include_terms),
        "exclude_terms": list(entry.exclude_terms),
        "minimum_quality_score": entry.minimum_quality_score,
        "accept_title_only": True,
        "allow_external_links": entry.allow_external_links,
        "allow_technical_updates": False,
        "requires_custom_collector": entry.implementation_status
        is ImplementationStatus.NEEDS_CUSTOM_COLLECTOR,
    }
    for field, value in values.items():
        setattr(source, field, value)
    return source


def _safe_legacy_match(source: Source, entry: SourceCatalogEntry) -> bool:
    if source.catalog_managed or source.origin is not SourceOrigin.PRESET:
        return False
    expected = _LEGACY_MANAGED_HASHES.get(entry.slug)
    if expected is None:
        return False
    payload = {field: _json_value(getattr(source, field)) for field in _LEGACY_MANAGED_FIELDS}
    return _hash(payload) == expected


def _legacy_alias_source(entry: SourceCatalogEntry, sources: Sequence[Source]) -> Source | None:
    alias = _LEGACY_ALIASES.get(entry.slug)
    if alias is None:
        return None
    _, url = alias
    return next((source for source in sources if source.start_url == url), None)


def _json_value(value: object) -> object:
    if hasattr(value, "value"):
        return cast(Any, value).value
    if isinstance(value, datetime):
        # SQLite stores these catalog timestamps without offsets.  The fingerprint
        # therefore compares the persisted wall-clock value, not an unavailable zone.
        return value.replace(tzinfo=None).isoformat()
    return value


def _hash(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SourceCatalogError(f"{location} must be a string-key mapping")
    result: dict[str, object] = {}
    raw = cast(Mapping[object, object], value)
    for key, item in raw.items():
        if not isinstance(key, str):
            raise SourceCatalogError(f"{location} must be a string-key mapping")
        result[key] = item
    return result


def _sequence(value: object, location: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SourceCatalogError(f"{location} must be a list")
    return cast(Sequence[object], value)


def _text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceCatalogError(f"{location} must be non-empty text")
    return value.strip()


def _optional_text(value: object, location: str) -> str | None:
    if value is None:
        return None
    return _text(value, location)


def _text_list(value: object, location: str) -> tuple[str, ...]:
    values = tuple(_text(item, location) for item in _sequence(value, location))
    if len(values) != len(set(values)):
        raise SourceCatalogError(f"{location} contains duplicates")
    return values


def _bool(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise SourceCatalogError(f"{location} must be boolean")
    return value


def _bounded_int(value: object, location: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SourceCatalogError(f"{location} must be between {minimum} and {maximum}")
    return value


def _score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
        raise SourceCatalogError("minimum_quality_score must be between 0 and 100")
    return float(value)


def _datetime_or_none(value: object, location: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise SourceCatalogError(f"{location} must include timezone")
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise SourceCatalogError(f"{location} must be ISO datetime") from exc
        if parsed.tzinfo is None:
            raise SourceCatalogError(f"{location} must include timezone")
        return parsed
    raise SourceCatalogError(f"{location} must be ISO datetime or null")


def _exact_keys(value: Mapping[str, object], expected: set[str], location: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        raise SourceCatalogError(
            f"{location} schema mismatch; missing={sorted(missing)} unknown={sorted(unknown)}"
        )
