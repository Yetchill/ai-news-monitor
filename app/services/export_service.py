"""Shared bounded query and rendering orchestration for Web and CLI exports."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.domain.exports import (
    EmptyExportError,
    ExportFormat,
    ExportGenerationError,
    ExportItem,
    ExportLimitExceededError,
    ExportMetadata,
    ExportQuery,
    ExportResult,
    InvalidExportLimitError,
)
from app.domain.queries import ItemFilter
from app.exporters.base import Exporter
from app.exporters.common import CATEGORY_LABELS
from app.storage.repositories import RepositoryUnitOfWork
from app.utils.url import is_http_url

UnitOfWorkFactory = Callable[[], RepositoryUnitOfWork]


class ExportService:
    """Apply the shared item query once, then delegate file rendering by format."""

    def __init__(self, uow_factory: UnitOfWorkFactory, exporters: Sequence[Exporter]) -> None:
        self._uow_factory = uow_factory
        self._exporters = {exporter.export_format: exporter for exporter in exporters}

    def export(self, export_format: ExportFormat, query: ExportQuery) -> ExportResult:
        exporter = self._exporters[export_format]
        limit = query.limit if query.limit is not None else exporter.max_items
        if limit < 1 or limit > exporter.max_items:
            raise InvalidExportLimitError(
                f"{export_format.value} 导出上限必须在 1 到 {exporter.max_items} 条之间。"
            )

        with self._uow_factory() as uow:
            rows = uow.items.list_filtered_with_sources(query.filters, limit=limit + 1)
            if not rows:
                raise EmptyExportError("当前筛选没有结果, 请调整范围后再导出。")
            if len(rows) > limit:
                raise ExportLimitExceededError(
                    f"当前筛选超过 {export_format.value} 导出上限 {limit} 条, 请缩小筛选范围。"
                )
            items = tuple(
                ExportItem(
                    id=item.id,
                    title=item.title,
                    summary=item.summary,
                    original_url=item.original_url if is_http_url(item.original_url) else None,
                    published_at=item.published_at,
                    discovered_at=item.discovered_at,
                    automatic_category=item.category,
                    manual_category=item.manual_category,
                    source_id=item.source_id,
                    source_name=source_name,
                    source_kind=source_kind,
                    is_favorite=item.is_favorite,
                    classification_score=item.classification_score,
                    classification_reason=item.classification_reason,
                    primary_type=item.primary_type,
                    manual_primary_type=item.manual_primary_type,
                    topic_tags=tuple(item.manual_topic_tags or item.topic_tags),
                    industry_tags=tuple(item.manual_industry_tags or item.industry_tags),
                    verification_status=item.verification_status,
                    review_status=item.review_status,
                    source_role=source_role,
                    discovery_url=(
                        item.discovery_url if is_http_url(item.discovery_url or "") else None
                    ),
                    official_url=(
                        item.official_url if is_http_url(item.official_url or "") else None
                    ),
                )
                for item, source_name, source_kind, source_role in rows
            )

        generated_at = datetime.now(UTC)
        metadata = ExportMetadata(
            generated_at=generated_at,
            filter_summary=_filter_summary(query.filters),
            item_count=len(items),
        )
        try:
            rendered = exporter.render(items, metadata)
        except Exception as exc:
            raise ExportGenerationError("导出文件生成失败, 请稍后重试。") from exc
        return ExportResult(
            content=rendered.content,
            filename=rendered.filename,
            ascii_filename=rendered.ascii_filename,
            media_type=rendered.media_type,
            item_count=len(items),
        )


def _filter_summary(item_filter: ItemFilter) -> str:
    parts: list[str] = []
    if item_filter.keyword:
        parts.append(f"关键词: {item_filter.keyword}")
    if item_filter.category is not None:
        parts.append(f"分类: {CATEGORY_LABELS[item_filter.category]}")
    if item_filter.source_id is not None:
        parts.append(f"来源 ID: {item_filter.source_id}")
    parts.append(f"来源范围: {item_filter.source_scope.value}")
    if item_filter.favorite is not None:
        parts.append("仅收藏" if item_filter.favorite else "未收藏")
    _append_range(parts, "发布时间", item_filter.published_from, item_filter.published_to)
    _append_range(parts, "发现时间", item_filter.discovered_from, item_filter.discovered_to)
    if item_filter.unclassified is not None:
        parts.append("仅待分类" if item_filter.unclassified else "排除待分类")
    return "; ".join(parts) if parts else "全部当前资讯"


def _append_range(
    parts: list[str],
    label: str,
    start: datetime | None,
    end: datetime | None,
) -> None:
    if start is not None:
        parts.append(f"{label}从 {start:%Y-%m-%d}")
    if end is not None:
        parts.append(f"{label}早于 {end:%Y-%m-%d}")
