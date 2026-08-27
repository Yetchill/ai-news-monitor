#!/usr/bin/env python3
"""Run the active catalog twice through the real pipeline and write a live report.

The report intentionally uses a brand-new temporary SQLite database. It never
loads application settings (and therefore never reads ``.env``), never calls an
AI provider, and records source failures instead of treating them as a script
failure. A non-zero exit means the report runner itself could not complete.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.domain.enums import LifecycleState, RunTrigger
from app.domain.models import Source
from app.domain.update import SourceUpdateResult
from app.fetchers.http import HttpFetcher
from app.services.classification_service import ClassificationService
from app.services.crawl_service import CrawlService
from app.services.source_catalog_service import SourceCatalogService
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork


@dataclass(frozen=True, slots=True)
class RoundReport:
    success: bool
    status: str
    duration_seconds: float
    discovered: int
    accepted: int
    new: int
    duplicate: int
    failure_category: str | None
    failure_reasons: dict[str, int]
    error: str | None


@dataclass(frozen=True, slots=True)
class SourceReport:
    name: str
    slug: str
    rounds: tuple[RoundReport, RoundReport]


def _uow_factory(database: Database) -> RepositoryUnitOfWork:
    return RepositoryUnitOfWork(database)


def _active_canonical_sources(database: Database) -> list[Source]:
    with _uow_factory(database) as uow:
        return sorted(
            (
                source
                for source in uow.sources.list()
                if source.catalog_managed
                and source.enabled
                and source.lifecycle_state is LifecycleState.ACTIVE
                and source.slug
            ),
            key=lambda source: source.slug or "",
        )


def _failure_category(result: SourceUpdateResult) -> str | None:
    if result.status.value == "success":
        return None
    error = (result.error or "").casefold()
    reasons = result.failure_reason_counts
    if "fetch.failed" in reasons:
        if "timeout" in error:
            return "timeout"
        if "http " in error or "rate limit" in error or "forbidden" in error:
            return "http"
        return "network"
    if "parse_or_collection.failed" in reasons:
        return "parse"
    return "other"


def _round_report(result: SourceUpdateResult, duration_seconds: float) -> RoundReport:
    return RoundReport(
        success=result.status.value == "success",
        status=result.status.value,
        duration_seconds=duration_seconds,
        discovered=result.discovered,
        accepted=result.accepted,
        new=result.new,
        duplicate=result.duplicate,
        failure_category=_failure_category(result),
        failure_reasons=dict(result.failure_reason_counts),
        error=result.error,
    )


async def _run_round(
    pipeline: UpdatePipeline,
    sources: list[Source],
) -> dict[int, RoundReport]:
    reports: dict[int, RoundReport] = {}
    for source in sources:
        started = time.perf_counter()
        update = await pipeline.update(source_id=source.id, trigger=RunTrigger.MANUAL_CLI)
        duration_seconds = time.perf_counter() - started
        if len(update.source_results) != 1:
            raise RuntimeError(f"source {source.slug} did not produce exactly one source result")
        reports[source.id] = _round_report(update.source_results[0], duration_seconds)
    return reports


async def _collect(*, timeout_seconds: float) -> tuple[dict[str, object], list[SourceReport]]:
    with tempfile.TemporaryDirectory(prefix="aim-live-source-report-") as temporary:
        database_path = Path(temporary) / "live-sources.db"
        database = Database(f"sqlite:///{database_path.as_posix()}")
        database.create_schema()
        try:

            def make_uow() -> RepositoryUnitOfWork:
                return _uow_factory(database)

            sync = SourceCatalogService(make_uow).sync()
            sources = _active_canonical_sources(database)
            classification = ClassificationService(RuleBasedClassifier.from_yaml(), make_uow)
            async with HttpFetcher(
                timeout_seconds=timeout_seconds,
                request_interval_seconds=0,
                max_retries=0,
            ) as fetcher:
                pipeline = UpdatePipeline(
                    uow_factory=make_uow,
                    crawl_service=CrawlService(default_collector_registry(), fetcher),
                    classification_service=classification,
                )
                first_round = await _run_round(pipeline, sources)
                second_round = await _run_round(pipeline, sources)
        finally:
            database.dispose()

    source_reports = [
        SourceReport(
            name=source.name,
            slug=source.slug or "",
            rounds=(first_round[source.id], second_round[source.id]),
        )
        for source in sources
    ]
    return asdict(sync), source_reports


def _summary(reports: list[SourceReport]) -> dict[str, object]:
    all_rounds = [round_report for report in reports for round_report in report.rounds]
    second_rounds = [report.rounds[1] for report in reports]
    failure_categories = Counter(
        round_report.failure_category
        for round_report in all_rounds
        if round_report.failure_category is not None
    )
    return {
        "active_total": len(reports),
        "source_rounds": len(all_rounds),
        "successful_rounds": sum(round_report.success for round_report in all_rounds),
        "failed_rounds": sum(not round_report.success for round_report in all_rounds),
        "failure_categories": {
            category: failure_categories[category]
            for category in ("timeout", "http", "network", "parse", "other")
        },
        "first_round": {
            "discovered": sum(report.rounds[0].discovered for report in reports),
            "accepted": sum(report.rounds[0].accepted for report in reports),
            "new": sum(report.rounds[0].new for report in reports),
        },
        "second_round": {
            "discovered": sum(round_report.discovered for round_report in second_rounds),
            "accepted": sum(round_report.accepted for round_report in second_rounds),
            "new": sum(round_report.new for round_report in second_rounds),
            "duplicate": sum(round_report.duplicate for round_report in second_rounds),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="report JSON output path")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=10.0,
        help="per-request timeout; retries are disabled to keep the live probe bounded",
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    sync, reports = asyncio.run(_collect(timeout_seconds=args.timeout_seconds))
    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "ai": {"classifier": "rule_based", "api_key_configured": False},
        "database": {"temporary": True, "repository_data_written": False},
        "catalog_sync": sync,
        "summary": _summary(reports),
        "sources": [asdict(report) for report in reports],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
