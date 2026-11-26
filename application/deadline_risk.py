"""Evaluate RR-01 and RR-02 from normalized inputs without reading Odoo."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import ClassVar

from .risk_status import RiskStatus, task_is_open


@dataclass(frozen=True, slots=True)
class DeadlineTaskSnapshot:
    """Supply task flags and a deadline whose precision is known to the caller.

    A flag of None means unknown. Use an aware datetime for an exact deadline,
    a date for a date-only deadline, and deadline_known=False for unreadable data.
    None with deadline_known=True means the task has no deadline.
    Example: DeadlineTaskSnapshot(True, False, None) describes an open task
    with no deadline and returns the corresponding snapshot.
    """

    active: bool | None
    is_closed: bool | None
    deadline: date | datetime | None
    deadline_known: bool = True


@dataclass(frozen=True, slots=True)
class DeadlineRiskResult:
    """Report RR-01 and, only on a signal, its calendar-day deviation.

    Zero days on a signal means the exact deadline expired earlier today.
    Example: DeadlineRiskResult(RiskStatus.TRIGGERED, 2) returns an RR-01
    result with two calendar days of delay.
    """

    rule_id: ClassVar[str] = "RR-01"
    status: RiskStatus
    overdue_days: int | None = None


@dataclass(frozen=True, slots=True)
class StalledDeadlineRiskResult:
    """Report RR-02 and the calendar-day counts supporting a signal.

    The inputs are the outcome, days until the deadline and days without
    movement. Example: StalledDeadlineRiskResult(RiskStatus.TRIGGERED, 3, 2)
    returns an RR-02 signal with three days left and two days without movement.
    """

    rule_id: ClassVar[str] = "RR-02"
    status: RiskStatus
    days_until_deadline: int | None = None
    days_without_movement: int | None = None


def _project_day(checked_at: datetime, project_timezone: tzinfo) -> date:
    """Return the project date of an offset-aware check instant.

    Example: _project_day(datetime(2026, 11, 25, tzinfo=UTC), UTC)
    returns date(2026, 11, 25).
    """
    if not isinstance(project_timezone, tzinfo):
        raise TypeError("A project time zone is required.")
    if checked_at.utcoffset() is None:
        raise ValueError("The check time must include a UTC offset.")
    return checked_at.astimezone(project_timezone).date()


def _compare_deadline(
    deadline: date | datetime, checked_at: datetime, project_timezone: tzinfo, checked_day: date,
) -> tuple[bool, int]:
    """Return whether an exact or date-only deadline expired and its day offset.

    The check instant must be aware, and checked_day is its project date.
    Example: _compare_deadline(date(2026, 11, 26), datetime(2026, 11, 25,
    tzinfo=UTC), UTC, date(2026, 11, 25)) returns (False, 1).
    """
    if isinstance(deadline, datetime):
        if deadline.utcoffset() is None:
            raise ValueError("An exact deadline must include a UTC offset.")
        # Compare instants, not wall times that may repeat during a clock change.
        expired: bool = checked_at.astimezone(UTC) > deadline.astimezone(UTC)
        return expired, (deadline.astimezone(project_timezone).date() - checked_day).days
    return checked_day > deadline, (deadline - checked_day).days


def evaluate_deadline_risk(
    task: DeadlineTaskSnapshot,
    *,
    checked_at: datetime,
    project_timezone: tzinfo,
) -> DeadlineRiskResult:
    """Evaluate RR-01 at a supplied instant, without consulting activity history.

    Take task flags and a normalized deadline, an offset-aware check instant
    and the project time zone. Return an RR-01 result with delay only on a
    signal. Date-only deadlines expire after the stated local day; exact
    deadlines expire strictly after the stated instant.

    Example: evaluate_deadline_risk(DeadlineTaskSnapshot(True, False, None),
    checked_at=datetime(2026, 11, 25, tzinfo=UTC), project_timezone=UTC)
    returns DeadlineRiskResult(RiskStatus.NOT_TRIGGERED).
    """
    checked_day: date = _project_day(checked_at, project_timezone)
    is_open: bool | None = task_is_open(task.active, task.is_closed)
    if is_open is False:
        return DeadlineRiskResult(RiskStatus.NOT_TRIGGERED)
    if is_open is None or not task.deadline_known:
        return DeadlineRiskResult(RiskStatus.NOT_EVALUATED)

    deadline: date | datetime | None = task.deadline
    if deadline is None:
        return DeadlineRiskResult(RiskStatus.NOT_TRIGGERED)
    expired: bool
    days_until_deadline: int
    expired, days_until_deadline = _compare_deadline(deadline, checked_at, project_timezone, checked_day)
    if not expired:
        return DeadlineRiskResult(RiskStatus.NOT_TRIGGERED)
    return DeadlineRiskResult(RiskStatus.TRIGGERED, -days_until_deadline)


def evaluate_stalled_deadline_risk(
    task: DeadlineTaskSnapshot,
    *,
    checked_at: datetime,
    project_timezone: tzinfo,
    last_moved_at: datetime | None,
    history_complete: bool,
    created_at: datetime | None = None,
) -> StalledDeadlineRiskResult:
    """Evaluate RR-02 from the deadline and confirmed movement facts.

    Take a normalized task, an aware check instant and the project time zone.
    Supply the latest observed stage or status change, not a comment or a
    generic update. Set history_complete only if no later transition could
    have been missed, or if the absence of any transition is confirmed. Use
    creation time only in the latter case. Timestamps must be offset-aware.
    Return an RR-02 result with day counts only when the rule triggers.

    Example: evaluate_stalled_deadline_risk(DeadlineTaskSnapshot(True, False,
    date(2026, 11, 28)), checked_at=datetime(2026, 11, 25, tzinfo=UTC),
    project_timezone=UTC, last_moved_at=datetime(2026, 11, 23, tzinfo=UTC),
    history_complete=True) returns StalledDeadlineRiskResult(RiskStatus.TRIGGERED, 3, 2).
    """
    checked_day: date = _project_day(checked_at, project_timezone)
    is_open: bool | None = task_is_open(task.active, task.is_closed)
    if is_open is False:
        return StalledDeadlineRiskResult(RiskStatus.NOT_TRIGGERED)

    days_until_deadline: int | None = None
    if task.deadline_known:
        if task.deadline is None:
            return StalledDeadlineRiskResult(RiskStatus.NOT_TRIGGERED)
        expired: bool
        expired, days_until_deadline = _compare_deadline(task.deadline, checked_at, project_timezone, checked_day)
        if expired or not 0 <= days_until_deadline <= 3:
            return StalledDeadlineRiskResult(RiskStatus.NOT_TRIGGERED)

    baseline: datetime | None = last_moved_at
    if baseline is None and history_complete:
        baseline = created_at
    if baseline is None:
        return StalledDeadlineRiskResult(RiskStatus.NOT_EVALUATED)
    if baseline.utcoffset() is None:
        raise ValueError("The movement or creation time must include a UTC offset.")
    if baseline.astimezone(UTC) > checked_at.astimezone(UTC):
        return StalledDeadlineRiskResult(RiskStatus.NOT_EVALUATED)

    days_without_movement: int = (checked_day - baseline.astimezone(project_timezone).date()).days
    if days_without_movement < 2:
        return StalledDeadlineRiskResult(RiskStatus.NOT_TRIGGERED)
    if is_open is None or days_until_deadline is None or not history_complete:
        return StalledDeadlineRiskResult(RiskStatus.NOT_EVALUATED)
    return StalledDeadlineRiskResult(RiskStatus.TRIGGERED, days_until_deadline, days_without_movement)
