"""Persistence-neutral content-admission decisions."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AdmissionRuleMatch:
    """One stable, machine-auditable rule contribution."""

    rule_id: str
    effect: Literal["accept", "reject", "score"]
    field: str
    value: str | None = None
    score_delta: int = 0


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Decision made before classification and persistence.

    ``quality_score`` is an integer in the inclusive 0..100 range.
    """

    accepted: bool
    reason: str
    matched_rules: tuple[AdmissionRuleMatch, ...]
    quality_score: int
