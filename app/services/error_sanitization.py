"""Short user-facing error rendering that excludes secrets and response bodies."""

import re
from urllib.parse import urlsplit, urlunsplit

_HTML_TAG = re.compile(r"<[^>]{0,500}>")
_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)([^\s,;]+)")
_URL = re.compile(r"https?://[^\s<>'\"]+")
_INTERNAL_TERM = re.compile(r"(?i)\b(?:traceback|sqlalchemy(?:error)?|python)\b")


def sanitize_error(error: BaseException | str, *, limit: int = 500) -> str:
    text = str(error)
    text = _HTML_TAG.sub(" ", text)
    text = _SECRET.sub(r"\1\2[REDACTED]", text)
    text = _URL.sub(_strip_url_query, text)
    text = _INTERNAL_TERM.sub("internal error", text)
    text = " ".join(text.split())
    if not text:
        text = type(error).__name__ if isinstance(error, BaseException) else "unknown error"
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def _strip_url_query(match: re.Match[str]) -> str:
    try:
        parts = urlsplit(match.group(0))
        hostname = parts.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname if parts.port is None else f"{hostname}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "[invalid URL]"
