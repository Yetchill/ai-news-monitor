"""CrawlRun lifecycle ownership and aggregate statistics."""

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.domain.enums import CrawlStatus, RunTrigger
from app.domain.models import CrawlRun, CrawlSourceExecution
from app.domain.update import SourceUpdateResult, SourceUpdateStatus, UpdateResult
from app.services.error_sanitization import sanitize_error
from app.storage.repositories import RepositoryUnitOfWork

UnitOfWorkFactory = Callable[[], RepositoryUnitOfWork]


class CrawlRunService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def start(self, *, source_total: int, trigger: RunTrigger = RunTrigger.MANUAL_CLI) -> int:
        with self._uow_factory() as uow:
            run = uow.crawl_runs.add(
                CrawlRun(
                    status=CrawlStatus.RUNNING,
                    started_at=datetime.now(UTC),
                    trigger=trigger,
                    source_total=source_total,
                )
            )
            return run.id

    def finish(
        self,
        crawl_run_id: int,
        source_results: Sequence[SourceUpdateResult],
        *,
        fatal_error: BaseException | str | None = None,
    ) -> UpdateResult:
        source_success = sum(
            result.status is SourceUpdateStatus.SUCCESS for result in source_results
        )
        source_failed = sum(result.status is SourceUpdateStatus.FAILED for result in source_results)
        status = _final_status(
            source_total=len(source_results),
            source_success=source_success,
            source_failed=source_failed,
            fatal=fatal_error is not None,
        )
        errors = [
            sanitize_error(f"{result.source_name}: {result.error}", limit=1000)
            for result in source_results
            if result.error
        ]
        if fatal_error is not None:
            errors.append(f"pipeline: {sanitize_error(fatal_error)}")
        error_summary = "; ".join(errors)[:2000] or None
        finished_at = datetime.now(UTC)

        with self._uow_factory() as uow:
            run = uow.crawl_runs.get(crawl_run_id)
            if run is None:
                raise LookupError(f"crawl run {crawl_run_id} does not exist")
            run.finished_at = finished_at
            run.status = status
            run.source_success = source_success
            run.source_failed = source_failed
            run.discovered_count = sum(result.discovered for result in source_results)
            run.new_count = sum(result.new for result in source_results)
            run.updated_count = sum(result.updated for result in source_results)
            run.skipped_count = sum(result.skipped for result in source_results)
            run.unclassified_count = sum(result.unclassified for result in source_results)
            run.normalized_count = sum(result.normalized for result in source_results)
            run.accepted_count = sum(result.accepted for result in source_results)
            run.rejected_count = sum(result.rejected for result in source_results)
            run.classified_count = sum(result.classified for result in source_results)
            run.duplicate_count = sum(result.duplicate for result in source_results)
            run.failed_count = sum(result.failed for result in source_results)
            reason_counts: Counter[str] = Counter()
            failure_counts: Counter[str] = Counter()
            for result in source_results:
                reason_counts.update(result.rejection_reason_counts)
                failure_counts.update(result.failure_reason_counts)
            run.rejection_reason_counts = dict(reason_counts)
            run.failure_reason_counts = dict(failure_counts)
            run.error_summary = error_summary
            for source_result in source_results:
                execution = uow.crawl_source_executions.get_by_run_source(
                    crawl_run_id, source_result.source_id
                )
                if execution is None:
                    execution = uow.crawl_source_executions.add(
                        CrawlSourceExecution(
                            crawl_run_id=crawl_run_id,
                            source_id=source_result.source_id,
                            status=source_result.status.value,
                        )
                    )
                execution.status = source_result.status.value
                execution.discovered_count = source_result.discovered
                execution.accepted_count = source_result.accepted
                execution.rejected_count = source_result.rejected
                execution.new_count = source_result.new
                execution.updated_count = source_result.updated
                execution.duplicate_count = source_result.duplicate
                execution.failed_count = source_result.failed
                execution.error = (
                    sanitize_error(source_result.error, limit=1000) if source_result.error else None
                )
            result = _to_result(run, source_results)
        return result


def _final_status(
    *,
    source_total: int,
    source_success: int,
    source_failed: int,
    fatal: bool,
) -> CrawlStatus:
    if fatal:
        return CrawlStatus.FAILED
    if source_total == 0 or source_failed == 0:
        return CrawlStatus.SUCCESS
    if source_success > 0:
        return CrawlStatus.PARTIAL_SUCCESS
    return CrawlStatus.FAILED


def _to_result(
    run: CrawlRun,
    source_results: Sequence[SourceUpdateResult],
) -> UpdateResult:
    if run.finished_at is None:
        raise RuntimeError("cannot return an unfinished crawl run")
    return UpdateResult(
        crawl_run_id=run.id,
        status=run.status,
        trigger=run.trigger,
        started_at=_utc(run.started_at),
        finished_at=_utc(run.finished_at),
        source_total=run.source_total,
        source_success=run.source_success,
        source_failed=run.source_failed,
        discovered_count=run.discovered_count,
        new_count=run.new_count,
        updated_count=run.updated_count,
        skipped_count=run.skipped_count,
        unclassified_count=run.unclassified_count,
        error_summary=run.error_summary,
        source_results=tuple(source_results),
        normalized_count=run.normalized_count,
        accepted_count=run.accepted_count,
        rejected_count=run.rejected_count,
        classified_count=run.classified_count,
        duplicate_count=run.duplicate_count,
        failed_count=run.failed_count,
        rejection_reason_counts=dict(run.rejection_reason_counts),
        failure_reason_counts=dict(run.failure_reason_counts),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
