"""Evaluate RR-01 for a normalized task snapshot without reading Odoo."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from enum import StrEnum
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class DeadlineTaskSnapshot:
    """Supply task flags and a deadline whose precision is known to the caller.

    A flag of None means unknown. Use an aware datetime for an exact deadline,
    a date for a date-only deadline, and deadline_known=False for unreadable data.
    None with deadline_known=True means the task has no deadline.
    """

    active: bool | None
    is_closed: bool | None
    deadline: date | datetime | None
    deadline_known: bool = True


class DeadlineRiskStatus(StrEnum):
    """Distinguish a signal from a negative or incomplete evaluation."""

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True, slots=True)
class DeadlineRiskResult:
    """Report RR-01 and, only on a signal, its calendar-day deviation.

    Zero days on a signal means the exact deadline expired earlier today.
    """

    rule_id: ClassVar[str] = "RR-01"
    status: DeadlineRiskStatus
    overdue_days: int | None = None


def evaluate_deadline_risk(
    task: DeadlineTaskSnapshot,
    *,
    checked_at: datetime,
    project_timezone: tzinfo,
) -> DeadlineRiskResult:
    """Evaluate RR-01 at a supplied instant, without consulting activity history.

    Treat date-only deadlines as valid through the stated local day. Exact
    deadlines expire strictly after the stated instant. Datetimes must carry
    an offset; the caller must normalize a raw Odoo Datetime before calling.
    """
    if not isinstance(project_timezone, tzinfo):
        raise TypeError("A project time zone is required.")
    if checked_at.utcoffset() is None:
        raise ValueError("The check time must include a UTC offset.")
    if task.active is False or task.is_closed is True:
        return DeadlineRiskResult(DeadlineRiskStatus.NOT_TRIGGERED)
    if task.active is None or task.is_closed is None or not task.deadline_known:
        return DeadlineRiskResult(DeadlineRiskStatus.NOT_EVALUATED)

    deadline: date | datetime | None = task.deadline
    if deadline is None:
        return DeadlineRiskResult(DeadlineRiskStatus.NOT_TRIGGERED)

    checked_day: date = checked_at.astimezone(project_timezone).date()
    deadline_day: date
    if isinstance(deadline, datetime):
        if deadline.utcoffset() is None:
            raise ValueError("An exact deadline must include a UTC offset.")
        # Compare instants, not wall times that may repeat during a clock change.
        if checked_at.astimezone(UTC) <= deadline.astimezone(UTC):
            return DeadlineRiskResult(DeadlineRiskStatus.NOT_TRIGGERED)
        deadline_day = deadline.astimezone(project_timezone).date()
    else:
        deadline_day = deadline
        if checked_day <= deadline_day:
            return DeadlineRiskResult(DeadlineRiskStatus.NOT_TRIGGERED)

    overdue_days: int = (checked_day - deadline_day).days
    return DeadlineRiskResult(DeadlineRiskStatus.TRIGGERED, overdue_days)
