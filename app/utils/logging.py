"""Application logging with size-based rotation and secret redaction."""

import logging
import re
from logging.handlers import RotatingFileHandler

from app.config import Settings, get_settings

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)([^\s,;]+)")


class SecretRedactionFilter(logging.Filter):
    """Redact common secret assignments before records reach any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        record.msg = SECRET_PATTERN.sub(r"\1\2[REDACTED]", rendered)
        record.args = ()
        return True


def _rotating_handler(
    filename: str,
    settings: Settings,
    *,
    level: int = logging.NOTSET,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        settings.log_dir / filename,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(SecretRedactionFilter())
    return handler


def configure_logging(settings: Settings | None = None) -> None:
    """Configure application, crawler, and error logs idempotently."""

    resolved = settings or get_settings()
    resolved.log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, resolved.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    console.addFilter(SecretRedactionFilter())
    root.addHandler(console)
    root.addHandler(_rotating_handler("application.log", resolved, level=level))
    root.addHandler(_rotating_handler("error.log", resolved, level=logging.ERROR))

    crawler = logging.getLogger("app.crawler")
    crawler.handlers.clear()
    crawler.setLevel(level)
    crawler.propagate = False
    crawler.addHandler(_rotating_handler("crawler.log", resolved, level=level))
    crawler.addHandler(_rotating_handler("error.log", resolved, level=logging.ERROR))
