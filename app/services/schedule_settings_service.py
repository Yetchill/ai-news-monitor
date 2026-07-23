"""Validated persistence and calendar calculations for local scheduling."""

import os
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone_name

from app.domain.enums import Weekday
from app.domain.models import ScheduleSettings
from app.domain.scheduling import ScheduleSettingsValue
from app.storage.repositories import RepositoryUnitOfWork

_WEEKDAYS = tuple(Weekday)


class ScheduleValidationError(ValueError):
    """Raised when runtime schedule input cannot be safely persisted."""


class ScheduleSettingsService:
    def __init__(
        self,
        uow_factory: Callable[[], RepositoryUnitOfWork],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now
        self._write_lock = Lock()

    def get(self) -> ScheduleSettingsValue:
        with self._write_lock, self._uow_factory() as uow:
            row = uow.schedule_settings.get_singleton()
            if row is None:
                return _default_value()
            return _to_value(row)

    def save(
        self,
        *,
        enabled: bool,
        hour: int,
        minute: int,
        days: Iterable[Weekday | str],
        timezone: str,
    ) -> ScheduleSettingsValue:
        parsed_days = parse_weekdays(days)
        validate_time(hour, minute)
        validate_timezone(timezone)
        with self._write_lock, self._uow_factory() as uow:
            row = uow.schedule_settings.add_singleton_if_missing(
                ScheduleSettings(
                    id=1,
                    schedule_enabled=enabled,
                    schedule_hour=hour,
                    schedule_minute=minute,
                    schedule_days_mask=weekdays_to_mask(parsed_days),
                    timezone=timezone,
                    updated_at=_aware_utc(self._now()),
                )
            )
            row.schedule_enabled = enabled
            row.schedule_hour = hour
            row.schedule_minute = minute
            row.schedule_days_mask = weekdays_to_mask(parsed_days)
            row.timezone = timezone
            row.updated_at = _aware_utc(self._now())
            return _to_value(row)

    def mark_scheduled_trigger(self, triggered_at: datetime) -> ScheduleSettingsValue:
        triggered_at = _aware_utc(triggered_at)
        with self._write_lock, self._uow_factory() as uow:
            row = uow.schedule_settings.get_singleton()
            if row is None:
                raise RuntimeError("schedule settings do not exist")
            if (
                row.last_scheduled_trigger_at is None
                or _database_utc(row.last_scheduled_trigger_at) < triggered_at
            ):
                row.last_scheduled_trigger_at = triggered_at
            return _to_value(row)


def next_scheduled_run(settings: ScheduleSettingsValue, after: datetime) -> datetime | None:
    """Return the first valid scheduled instant strictly after ``after`` in UTC."""

    if not settings.enabled:
        return None
    after = _aware_utc(after)
    zone = validate_timezone(settings.timezone)
    local_after = after.astimezone(zone)
    selected = {day_index(day) for day in settings.days}
    for offset in range(0, 370):
        candidate_date = local_after.date() + timedelta(days=offset)
        if candidate_date.weekday() not in selected:
            continue
        candidate = _wall_time_to_utc(candidate_date, settings.hour, settings.minute, zone)
        if candidate is not None and candidate > after:
            return candidate
    raise RuntimeError("could not calculate the next scheduled run")


def parse_time(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if (
        len(value) != 5
        or value[2] != ":"
        or len(parts) != 2
        or any(not part.isascii() or not part.isdigit() for part in parts)
    ):
        raise ScheduleValidationError("执行时间必须使用 HH:MM 格式。")
    hour, minute = (int(part) for part in parts)
    validate_time(hour, minute)
    return hour, minute


def validate_time(hour: int, minute: int) -> None:
    if (
        isinstance(hour, bool)
        or isinstance(minute, bool)
        or not 0 <= hour <= 23
        or not 0 <= minute <= 59
    ):
        raise ScheduleValidationError("执行时间无效, 小时应为 0-23, 分钟应为 0-59。")


def parse_weekdays(values: Iterable[Weekday | str]) -> tuple[Weekday, ...]:
    parsed: set[Weekday] = set()
    try:
        for value in values:
            parsed.add(value if isinstance(value, Weekday) else Weekday(value.strip().lower()))
    except (ValueError, AttributeError) as exc:
        raise ScheduleValidationError("星期无效, 请使用 mon,tue,wed,thu,fri,sat,sun。") from exc
    if not parsed:
        raise ScheduleValidationError("请至少选择一个执行星期。")
    return tuple(day for day in _WEEKDAYS if day in parsed)


def validate_timezone(name: str) -> ZoneInfo:
    if not name or len(name) > 100 or name != name.strip():
        raise ScheduleValidationError("时区无效, 请填写 IANA 时区名称, 例如 Asia/Shanghai。")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleValidationError(f"无效的 IANA 时区: {name}") from exc


def system_timezone_name() -> str:
    """Return the system local timezone as a validated IANA name."""

    try:
        localzone_name = get_localzone_name()
    except Exception:
        localzone_name = None
    candidates = (os.environ.get("TZ"), localzone_name, "UTC")
    for candidate in candidates:
        if candidate and (candidate == "UTC" or "/" in candidate):
            try:
                validate_timezone(candidate)
            except ScheduleValidationError:
                continue
            return candidate
    return "UTC"


def weekdays_to_mask(days: Iterable[Weekday]) -> int:
    return sum(1 << day_index(day) for day in set(days))


def mask_to_weekdays(mask: int) -> tuple[Weekday, ...]:
    if not 1 <= mask <= 127:
        raise ScheduleValidationError("数据库中的星期设置无效。")
    return tuple(day for day in _WEEKDAYS if mask & (1 << day_index(day)))


def day_index(day: Weekday) -> int:
    return _WEEKDAYS.index(day)


def _wall_time_to_utc(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime | None:
    naive = datetime(day.year, day.month, day.day, hour, minute)
    candidates: set[datetime] = set()
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        candidate = local.astimezone(UTC)
        round_trip = candidate.astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive and round_trip.fold == fold:
            candidates.add(candidate)
    return max(candidates) if candidates else None


def _to_value(row: ScheduleSettings) -> ScheduleSettingsValue:
    return ScheduleSettingsValue(
        enabled=row.schedule_enabled,
        hour=row.schedule_hour,
        minute=row.schedule_minute,
        days=mask_to_weekdays(row.schedule_days_mask),
        timezone=row.timezone,
        updated_at=_database_utc(row.updated_at),
        last_scheduled_trigger_at=(
            _database_utc(row.last_scheduled_trigger_at) if row.last_scheduled_trigger_at else None
        ),
    )


def _default_value() -> ScheduleSettingsValue:
    return ScheduleSettingsValue(
        enabled=False,
        hour=9,
        minute=0,
        days=tuple(Weekday),
        timezone=system_timezone_name(),
        updated_at=None,
        last_scheduled_trigger_at=None,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleValidationError("调度时间必须包含明确时区。")
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
