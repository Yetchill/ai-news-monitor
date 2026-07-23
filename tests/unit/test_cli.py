"""Safety behavior for the minimal stage-four CLI adapter."""

from datetime import UTC, datetime
from typing import cast

import pytest

from app import cli
from app.domain.enums import CrawlStatus, RunTrigger
from app.domain.update import SourceUpdateResult, SourceUpdateStatus, UpdateResult
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork


class DisposableDatabase:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_taxonomy_reclassify_accepts_explicit_dry_run() -> None:
    arguments = cli._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["taxonomy", "reclassify", "--dry-run"]
    )

    assert arguments.command == "taxonomy"
    assert arguments.taxonomy_command == "reclassify"
    assert arguments.dry_run is True
    assert arguments.confirm is False


def test_update_exception_returns_nonzero_without_traceback_or_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = DisposableDatabase()

    async def fail_update(_database: Database, _arguments: object) -> int:
        raise RuntimeError("source missing token=super-secret <html>response</html>")

    def fake_from_settings(_cls: type[Database]) -> Database:
        return cast(Database, database)

    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr(
        cli.Database,
        "from_settings",
        classmethod(fake_from_settings),
    )
    monkeypatch.setattr(cli, "_run_update", fail_update)

    exit_code = cli.main(["update", "--source-id", "999"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "error: source missing" in captured.err
    assert "super-secret" not in captured.err
    assert "<html>" not in captured.err
    assert "Traceback" not in captured.err
    assert database.disposed is True


def test_all_sources_failed_result_returns_nonzero(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    failed_result = UpdateResult(
        crawl_run_id=1,
        status=CrawlStatus.FAILED,
        started_at=now,
        finished_at=now,
        source_total=1,
        source_success=0,
        source_failed=1,
        discovered_count=0,
        new_count=0,
        updated_count=0,
        skipped_count=0,
        unclassified_count=0,
        error_summary="failed",
        source_results=(
            SourceUpdateResult(
                source_id=1,
                source_name="Feed",
                status=SourceUpdateStatus.FAILED,
                error="network failed",
            ),
        ),
    )

    class FailedPipeline:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def update(self, **_kwargs: object) -> UpdateResult:
            return failed_result

    monkeypatch.setattr(cli, "UpdatePipeline", FailedPipeline)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    def fake_from_settings(_cls: type[Database]) -> Database:
        return database

    monkeypatch.setattr(cli.Database, "from_settings", classmethod(fake_from_settings))

    exit_code = cli.main(["update"])

    assert exit_code == 1
    assert "status=failed" in capsys.readouterr().out


def test_cli_update_uses_execution_service_and_records_manual_cli_trigger(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    def fake_from_settings(_cls: type[Database]) -> Database:
        return database

    monkeypatch.setattr(cli.Database, "from_settings", classmethod(fake_from_settings))

    assert cli.main(["update"]) == 0
    assert "status=success" in capsys.readouterr().out
    with RepositoryUnitOfWork(database) as uow:
        runs = uow.crawl_runs.list_recent()
    assert len(runs) == 1
    assert runs[0].trigger is RunTrigger.MANUAL_CLI
