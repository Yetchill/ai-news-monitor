"""Logging configuration tests."""

import logging
from pathlib import Path

from app.config import Settings
from app.utils.logging import configure_logging


def test_logging_creates_expected_files_and_redacts_secrets(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        log_dir=tmp_path / "logs",
        log_max_bytes=1024,
        log_backup_count=2,
    )
    configure_logging(settings)

    logging.getLogger("app.test").error("api_key=super-secret")
    logging.getLogger("app.crawler").info("crawl complete")

    application_log = (settings.log_dir / "application.log").read_text(encoding="utf-8")
    crawler_log = (settings.log_dir / "crawler.log").read_text(encoding="utf-8")
    error_log = (settings.log_dir / "error.log").read_text(encoding="utf-8")
    assert "super-secret" not in application_log
    assert "api_key=[REDACTED]" in application_log
    assert "crawl complete" in crawler_log
    assert "super-secret" not in error_log
