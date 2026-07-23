"""Persistence-neutral runtime scheduling values."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.domain.enums import Weekday


class SchedulerStatus(StrEnum):
    DISABLED = "disabled"
    WAITING = "waiting"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ScheduleSettingsValue:
    enabled: bool
    hour: int
    minute: int
    days: tuple[Weekday, ...]
    timezone: str
    updated_at: datetime | None
    last_scheduled_trigger_at: datetime | None


@dataclass(frozen=True, slots=True)
class ScheduleView:
    settings: ScheduleSettingsValue
    next_run_at: datetime | None
    status: SchedulerStatus
    error: str | None = None
