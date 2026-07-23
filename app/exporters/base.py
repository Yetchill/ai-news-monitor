"""Exporter boundary independent from persistence and delivery adapters."""

from collections.abc import Sequence
from typing import Protocol

from app.domain.exports import ExportFormat, ExportItem, ExportMetadata, RenderedExport


class Exporter(Protocol):
    export_format: ExportFormat
    max_items: int

    def render(
        self,
        items: Sequence[ExportItem],
        metadata: ExportMetadata,
    ) -> RenderedExport: ...
