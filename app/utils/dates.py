"""Best-effort date parsing for feed and list metadata."""

from datetime import UTC, datetime

import dateparser


def parse_datetime(value: str | None, *, relative_base: datetime | None = None) -> datetime | None:
    """Parse a human-facing date and normalize aware results to UTC."""

    if not value or not value.strip():
        return None
    settings: dict[str, object] = {
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": "UTC",
        "TO_TIMEZONE": "UTC",
        "PREFER_DAY_OF_MONTH": "first",
        "PREFER_DATES_FROM": "past",
    }
    if relative_base is not None:
        settings["RELATIVE_BASE"] = relative_base
    parsed = dateparser.parse(
        value,
        settings=settings,
    )
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
