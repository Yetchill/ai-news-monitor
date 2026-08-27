"""Minimal development CLI for the shared stage-four update pipeline."""

import argparse
import asyncio
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.config.settings import get_settings
from app.domain.enums import Category, CrawlStatus, RunTrigger, SourceScope
from app.domain.exports import ExportFormat, ExportQuery
from app.domain.queries import ItemFilter
from app.domain.update import UpdateMode, UpdateResult
from app.services.application_factory import build_export_service, update_pipeline_context
from app.services.classification_service import ClassificationService
from app.services.error_sanitization import sanitize_error
from app.services.retired_source_purge import RetiredSourcePurgeService
from app.services.schedule_settings_service import (
    ScheduleSettingsService,
    next_scheduled_run,
    parse_time,
    parse_weekdays,
)
from app.services.scheduler_service import SchedulerService
from app.services.source_catalog_service import SourceCatalogService
from app.services.source_lifecycle_service import SourceLifecycleService
from app.services.update_execution_service import UpdateExecutionService, UpdateLock
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.migrations.runtime import ensure_database_current
from app.storage.repositories import RepositoryUnitOfWork
from app.utils.logging import configure_logging


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    database: Database | None = None
    try:
        configure_logging()
        database = Database.from_settings()
        if hasattr(database, "database_url") and hasattr(database, "engine"):
            ensure_database_current(database)
        if arguments.command == "runs":
            _print_runs(database, arguments.limit)
            return 0
        if arguments.command == "sources":
            return asyncio.run(_run_sources_command(database, arguments))
        if arguments.command == "taxonomy":
            _run_taxonomy_command(database, arguments)
            return 0
        if arguments.command == "export":
            _run_export(database, arguments)
            return 0
        if arguments.command == "schedule":
            return _run_schedule_command(database, arguments)
        return asyncio.run(_run_update(database, arguments))
    except KeyboardInterrupt:
        print("scheduler stopped")
        return 0
    except Exception as exc:
        print(f"error: {sanitize_error(exc)}", file=sys.stderr)
        return 2
    finally:
        if database is not None:
            database.dispose()


async def _run_update(database: Database, arguments: argparse.Namespace) -> int:
    execution = UpdateExecutionService(
        database,
        lambda target: update_pipeline_context(target, UpdatePipeline),
        UpdateLock(),
    )
    result = await execution.update(
        trigger=RunTrigger.MANUAL_CLI,
        source_id=arguments.source_id,
        allow_disabled=arguments.allow_disabled,
        formal_only=False,
        mode=UpdateMode(arguments.mode),
        max_pages=arguments.max_pages,
        max_items=arguments.max_items,
        published_from=_parse_datetime(arguments.published_from),
        published_to=_parse_datetime(arguments.published_to),
    )
    _print_result(result)
    return 1 if result.status is CrawlStatus.FAILED else 0


async def _run_sources_command(database: Database, arguments: argparse.Namespace) -> int:
    action = arguments.source_command
    catalog = SourceCatalogService(lambda: RepositoryUnitOfWork(database))
    if action in {"seed-formal", "seed", "sync-catalog"}:
        result = catalog.sync(reconcile=bool(getattr(arguments, "reconcile", False)))
        implementation = " ".join(
            f"{key}={value}" for key, value in sorted(result.implementation_counts.items())
        )
        print(
            f"catalog total={result.total} created={result.created} updated={result.updated} "
            f"existing={result.existing} conflicts={result.conflicts} deleted={result.deleted} "
            f"retired={result.retired} active={result.active} "
            f"candidate={result.candidate} paused={result.paused} {implementation}"
        )
        return 1 if result.conflicts else 0
    if action == "catalog":
        entries = catalog.load()
        with RepositoryUnitOfWork(database) as uow:
            by_slug = {source.slug: source for source in uow.sources.list()}
        selected = 0
        for entry in entries:
            source = by_slug.get(entry.slug)
            state = source.lifecycle_state.value if source is not None else "not_synced"
            implementation = (
                source.implementation_status.value
                if source is not None
                else entry.implementation_status.value
            )
            if arguments.state and state != arguments.state:
                continue
            if arguments.role and entry.source_role.value != arguments.role:
                continue
            if (
                arguments.implementation_status
                and implementation != arguments.implementation_status
            ):
                continue
            selected += 1
            reason = (
                source.implementation_reason if source is not None else entry.implementation_reason
            )
            print(
                f"{entry.slug}\t{state}\t{entry.source_role.value}\t{entry.crawl_mode.value}\t"
                f"{implementation}\t{entry.url}\t{reason}"
            )
        print(f"catalog rows={selected}/{len(entries)}")
        return 0
    registry = default_collector_registry()
    lifecycle = SourceLifecycleService(lambda: RepositoryUnitOfWork(database), registry.names())
    if action in {"preview", "activate"}:
        source = lifecycle.get_by_slug(arguments.slug)
        execution = UpdateExecutionService(
            database,
            lambda target: update_pipeline_context(target, UpdatePipeline),
            UpdateLock(),
        )
        result = await execution.preview(source.id, max_items=arguments.max_items)
        lifecycle.record_preview(arguments.slug, result)
        _print_preview(result, max_samples=arguments.max_items)
        if action == "activate":
            activated = lifecycle.activate(arguments.slug, result, confirm=arguments.confirm)
            print(
                f"activated slug={activated.slug} lifecycle={activated.lifecycle_state.value} "
                f"enabled={str(activated.enabled).lower()}"
            )
        return 1 if result.status.value == "failed" else 0
    if action == "purge-retired":
        service = RetiredSourcePurgeService(database, lambda: RepositoryUnitOfWork(database))
        result = (
            service.purge(confirm=True, backup_path=arguments.backup)
            if arguments.confirm
            else service.plan()
        )
        _print_purge(result)
        return 0
    raise ValueError(f"unknown sources command: {action}")


def _print_preview(result: object, *, max_samples: int) -> None:
    from app.domain.update import SourcePreviewResult

    preview = result if isinstance(result, SourcePreviewResult) else None
    if preview is None:
        raise TypeError("invalid preview result")
    print(
        f"preview source={preview.source_id}:{preview.source_name} status={preview.status.value} "
        f"fetch={preview.fetch_status} parse={preview.parse_status} extracted={preview.fetched} "
        f"normalized={preview.normalized} accepted={preview.accepted} rejected={preview.rejected} "
        f"failed={preview.failed}"
    )
    print(
        f"primary_types={preview.primary_type_counts} "
        f"verification={preview.verification_status_counts} "
        f"review={preview.review_status_counts} valid_title={preview.valid_title_ratio:.1%} "
        f"valid_date={preview.valid_date_ratio:.1%} valid_link={preview.valid_link_ratio:.1%} "
        f"external_link={preview.external_link_ratio:.1%} "
        f"duplicates={preview.duplicate_count} duplicate_ratio={preview.duplicate_ratio:.1%}"
    )
    print(
        f"rejection_reasons={preview.rejection_reason_counts} "
        f"failure_reasons={preview.failure_reason_counts}"
    )
    if preview.error:
        print(f"error={preview.error}")
    for index, item in enumerate(preview.items[:max_samples], start=1):
        print(
            f"sample={index} date={item.published_at.isoformat() if item.published_at else '-'} "
            f"type={item.primary_type.value} domain={item.link_domain or '-'} "
            f"accepted={str(item.accepted).lower()} reason={item.reason} title={item.title}"
        )


def _print_purge(result: object) -> None:
    from app.services.retired_source_purge import RetiredSourcePurgeResult

    purge = result if isinstance(result, RetiredSourcePurgeResult) else None
    if purge is None:
        raise TypeError("invalid purge result")
    print(f"purge mode={'dry-run' if purge.dry_run else 'confirm'} targets={len(purge.impacts)}")
    for impact in purge.impacts:
        print(
            f"source={impact.source_id}:{impact.slug or '-'} "
            f"name={impact.name} url={impact.start_url} "
            f"items={impact.item_count} related={impact.related_record_count} "
            f"revisions={impact.revision_count} review_events={impact.review_event_count} "
            f"crawl_executions={impact.crawl_execution_count} "
            f"discovery_references={impact.discovery_reference_count}"
        )
    if not purge.dry_run:
        print(
            f"deleted sources={purge.deleted_sources} items={purge.deleted_items} "
            f"executions={purge.deleted_executions} empty_runs={purge.deleted_empty_runs} "
            f"cleaned_discoveries={purge.cleaned_discovery_references} backup={purge.backup_path}"
        )


def _run_taxonomy_command(database: Database, arguments: argparse.Namespace) -> None:
    service = ClassificationService(
        RuleBasedClassifier.from_yaml(), lambda: RepositoryUnitOfWork(database)
    )
    summary = (
        service.apply_v2_reclassification(source_id=arguments.source_id)
        if arguments.confirm
        else service.preview_v2_reclassification(source_id=arguments.source_id)
    )
    print(
        f"taxonomy reclassify mode={'confirm' if summary.applied else 'dry-run'} "
        f"total={summary.total} changed={summary.changed} unclassified={summary.unclassified} "
        f"preserved_manual={summary.preserved_manual}"
    )


def _print_result(result: UpdateResult) -> None:
    print(
        f"run={result.crawl_run_id} status={result.status.value} "
        f"sources={result.source_success}/{result.source_total} "
        f"discovered={result.discovered_count} new={result.new_count} "
        f"updated={result.updated_count} skipped={result.skipped_count} "
        f"accepted={result.accepted_count} rejected={result.rejected_count} "
        f"unclassified={result.unclassified_count}"
    )
    if result.source_total == 0:
        print("no enabled sources were selected")
    for source in result.source_results:
        source_name = sanitize_error(source.source_name, limit=255)
        suffix = f" error={sanitize_error(source.error)}" if source.error else ""
        print(
            f"source={source.source_id}:{source_name} status={source.status.value} "
            f"fetched={source.discovered} normalized={source.normalized} "
            f"accepted={source.accepted} rejected={source.rejected} "
            f"classified={source.classified} inserted={source.new} updated={source.updated} "
            f"duplicate={source.duplicate} failed={source.failed} "
            f"rejection_reasons={source.rejection_reason_counts or {}} "
            f"failure_reasons={source.failure_reason_counts or {}}{suffix}"
        )


def _print_runs(database: Database, limit: int) -> None:
    with RepositoryUnitOfWork(database) as uow:
        runs = uow.crawl_runs.list_recent(limit)
    if not runs:
        print("no crawl runs found")
        return
    for run in runs:
        print(
            f"run={run.id} status={run.status.value} trigger={run.trigger.value} "
            f"started={run.started_at.isoformat()} "
            f"finished={run.finished_at.isoformat() if run.finished_at else '-'} "
            f"sources={run.source_success}/{run.source_total} new={run.new_count} "
            f"updated={run.updated_count} skipped={run.skipped_count}"
        )


def _run_schedule_command(database: Database, arguments: argparse.Namespace) -> int:
    service = ScheduleSettingsService(lambda: RepositoryUnitOfWork(database))
    action = arguments.schedule_command
    if action == "show":
        _print_schedule(service)
        return 0
    current = service.get()
    if action == "disable":
        service.save(
            enabled=False,
            hour=current.hour,
            minute=current.minute,
            days=current.days,
            timezone=current.timezone,
        )
        _print_schedule(service)
        return 0
    if action == "enable":
        hour, minute = parse_time(arguments.schedule_time)
        service.save(
            enabled=True,
            hour=hour,
            minute=minute,
            days=parse_weekdays(arguments.days.split(",")),
            timezone=arguments.timezone or current.timezone,
        )
        _print_schedule(service)
        return 0
    if action == "run":
        if not current.enabled:
            raise ValueError(
                "schedule is disabled; enable it before running the foreground scheduler"
            )
        return asyncio.run(_run_foreground_scheduler(database, service))
    raise ValueError(f"unknown schedule command: {action}")


def _print_schedule(service: ScheduleSettingsService) -> None:
    settings = service.get()
    next_run = next_scheduled_run(settings, datetime.now(UTC))
    print(
        f"enabled={'true' if settings.enabled else 'false'} "
        f"time={settings.hour:02d}:{settings.minute:02d} "
        f"days={','.join(day.value for day in settings.days)} timezone={settings.timezone}"
    )
    print(f"next={next_run.isoformat() if next_run else '-'}")
    last_trigger = (
        settings.last_scheduled_trigger_at.isoformat()
        if settings.last_scheduled_trigger_at
        else "-"
    )
    print(f"last_scheduled_trigger={last_trigger}")


async def _run_foreground_scheduler(database: Database, settings: ScheduleSettingsService) -> int:
    updates = UpdateExecutionService(database, update_pipeline_context, UpdateLock())
    scheduler = SchedulerService(settings, updates)
    await scheduler.start()
    print("foreground scheduler running; press Ctrl+C to stop")
    try:
        await asyncio.Event().wait()
    finally:
        await scheduler.stop()
    return 0


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO datetime: {value}") from exc


def _run_export(database: Database, arguments: argparse.Namespace) -> None:
    published_from = _parse_export_date(arguments.published_from)
    published_to = _parse_export_date(arguments.published_to, exclusive_end=True)
    discovered_from = _parse_export_date(arguments.discovered_from)
    discovered_to = _parse_export_date(arguments.discovered_to, exclusive_end=True)
    if published_from and published_to and published_from >= published_to:
        raise ValueError("published-from must not be later than published-to")
    if discovered_from and discovered_to and discovered_from >= discovered_to:
        raise ValueError("discovered-from must not be later than discovered-to")

    export_format = ExportFormat(arguments.export_format)
    result = build_export_service(database).export(
        export_format,
        ExportQuery(
            filters=ItemFilter(
                keyword=arguments.query,
                category=Category(arguments.category) if arguments.category else None,
                source_id=arguments.source_id,
                favorite=arguments.favorite,
                published_from=published_from,
                published_to=published_to,
                discovered_from=discovered_from,
                discovered_to=discovered_to,
                unclassified=arguments.unclassified,
                source_scope=(
                    SourceScope.INDUSTRY_LEADS
                    if arguments.industry_leads
                    else SourceScope.FORMAL_EXPORT
                ),
            ),
            limit=arguments.limit,
        ),
    )
    output = arguments.output or (get_settings().output_dir / result.filename)
    expected_suffix = ".xlsx" if export_format is ExportFormat.EXCEL else ".docx"
    if output.suffix.lower() != expected_suffix:
        raise ValueError(f"output path must end with {expected_suffix}")
    _atomic_write(output, result.content, force=arguments.force)
    print(f"exported {result.item_count} items to {output}")


def _parse_export_date(value: str | None, *, exclusive_end: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
        if exclusive_end:
            parsed += timedelta(days=1)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"invalid export date: {value}; expected YYYY-MM-DD") from exc
    return datetime.combine(parsed, time.min, UTC)


def _atomic_write(path: Path, content: bytes, *, force: bool) -> None:
    path = path.expanduser()
    _validate_output_target(path, force=force)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        _validate_output_target(path, force=force)
        if force:
            os.replace(temporary_path, path)
            temporary_path = None
        else:
            os.link(temporary_path, path)
            temporary_path.unlink()
            temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _validate_output_target(path: Path, *, force: bool) -> None:
    if path.is_symlink():
        raise ValueError("output path must not be a symbolic link")
    if path.exists() and path.is_dir():
        raise ValueError("output path must be a file, not a directory")
    if path.exists() and not force:
        raise FileExistsError(17, "File exists", path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI intelligence update pipeline debug CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser("update", help="update enabled sources")
    update.add_argument("--source-id", type=int)
    update.add_argument("--allow-disabled", action="store_true")
    update.add_argument(
        "--all-enabled",
        action="store_true",
        help="include enabled test/fallback sources; bulk updates select formal sources by default",
    )
    update.add_argument(
        "--mode",
        choices=[mode.value for mode in UpdateMode],
        default="incremental",
    )
    update.add_argument("--max-pages", type=int)
    update.add_argument("--max-items", type=int)
    update.add_argument("--from", dest="published_from")
    update.add_argument("--to", dest="published_to")
    runs = commands.add_parser("runs", help="show recent crawl run summaries")
    runs.add_argument("--limit", type=int, default=5)
    sources = commands.add_parser("sources", help="manage preset source configuration")
    source_commands = sources.add_subparsers(dest="source_command", required=True)
    source_commands.add_parser("seed-formal", help="idempotently initialize formal sources")
    source_commands.add_parser("seed", help=argparse.SUPPRESS)
    sync_catalog = source_commands.add_parser(
        "sync-catalog", help="sync every catalog source into the database"
    )
    sync_catalog.add_argument(
        "--reconcile",
        action="store_true",
        help="make preset rows match the canonical catalog and safely retire legacy rows",
    )
    catalog = source_commands.add_parser(
        "catalog", help="show catalog and database lifecycle state"
    )
    catalog.add_argument("--state", choices=["active", "candidate", "paused", "not_synced"])
    catalog.add_argument("--role")
    catalog.add_argument("--implementation-status")
    preview = source_commands.add_parser(
        "preview", help="preview one source without item persistence"
    )
    preview.add_argument("slug")
    preview.add_argument("--max-items", type=_positive_int, default=20)
    preview.add_argument(
        "--no-persist",
        action="store_true",
        help="explicitly document that preview never writes items or a formal crawl run",
    )
    activate = source_commands.add_parser("activate", help="preview then activate one candidate")
    activate.add_argument("slug")
    activate.add_argument("--max-items", type=_positive_int, default=20)
    activate.add_argument("--confirm", action="store_true")
    purge = source_commands.add_parser("purge-retired", help="purge exact retired source records")
    purge.add_argument("--confirm", action="store_true")
    purge.add_argument("--dry-run", action="store_true")
    purge.add_argument("--backup", type=Path)
    export = commands.add_parser("export", help="export filtered intelligence")
    export_formats = export.add_subparsers(dest="export_format", required=True)
    for export_format in ExportFormat:
        command = export_formats.add_parser(export_format.value)
        command.add_argument("--output", type=Path)
        command.add_argument("--category", choices=[category.value for category in Category])
        command.add_argument("--source-id", type=_positive_int)
        command.add_argument("--favorite", action="store_const", const=True, default=None)
        command.add_argument("--published-from")
        command.add_argument("--published-to")
        command.add_argument("--discovered-from")
        command.add_argument("--discovered-to")
        command.add_argument("--query")
        command.add_argument("--unclassified", action="store_const", const=True, default=None)
        command.add_argument("--limit", type=_positive_int)
        command.add_argument("--force", action="store_true")
        command.add_argument(
            "--industry-leads",
            action="store_true",
            help="explicitly export the industry-lead/review queue with verification metadata",
        )
    schedule = commands.add_parser("schedule", help="view or manage runtime scheduling")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_commands.add_parser("show", help="show current schedule settings")
    enable = schedule_commands.add_parser("enable", help="enable and configure scheduling")
    enable.add_argument("--time", dest="schedule_time", required=True, metavar="HH:MM")
    enable.add_argument("--days", required=True, help="comma-separated mon,tue,...,sun")
    enable.add_argument("--timezone", help="IANA timezone; preserves current value when omitted")
    schedule_commands.add_parser("disable", help="disable scheduling")
    schedule_commands.add_parser("run", help="run the scheduler in the foreground")
    taxonomy = commands.add_parser("taxonomy", help="taxonomy-v2 maintenance")
    taxonomy_commands = taxonomy.add_subparsers(dest="taxonomy_command", required=True)
    reclassify = taxonomy_commands.add_parser("reclassify", help="dry-run/apply taxonomy v2")
    reclassify.add_argument("--source-id", type=_positive_int)
    reclassify_mode = reclassify.add_mutually_exclusive_group()
    reclassify_mode.add_argument("--dry-run", action="store_true")
    reclassify_mode.add_argument("--confirm", action="store_true")
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
