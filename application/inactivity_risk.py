"""Evaluate RR-04 from confirmed activity facts without querying Odoo."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import ClassVar

from .project_time import project_day
from .risk_status import RiskStatus, task_is_open


@dataclass(frozen=True, slots=True)
class ActivitySource:
    """Store the latest observed qualifying event and coverage of its source.

    Supply an offset-aware timestamp or None, and mark coverage complete only
    if a later event could not have been missed, or if the source is known empty.
    Example: ActivitySource(None, False) returns an unknown activity source.
    """

    latest_at: datetime | None
    complete: bool


@dataclass(frozen=True, slots=True)
class InactivityRiskResult:
    """Report RR-04 with the outcome and, for a signal, days without activity.

    Example: InactivityRiskResult(RiskStatus.TRIGGERED, 7) returns a signal
    backed by seven calendar days without activity.
    """

    rule_id: ClassVar[str] = "RR-04"
    status: RiskStatus
    days_without_activity: int | None = None


def evaluate_inactivity_risk(
    *,
    active: bool | None,
    is_closed: bool | None,
    checked_at: datetime,
    project_timezone: tzinfo,
    movement: ActivitySource,
    comments: ActivitySource,
    timesheets: ActivitySource | None,
    created_at: datetime | None = None,
) -> InactivityRiskResult:
    """Evaluate RR-04 at an aware check time on the project calendar.

    Pass task flags (None if unknown) and activity sources for stage/status
    changes, human comments and registration of time entries. Do not pass
    automated messages, deadline updates or the work date of a time entry.
    Use timesheets=None only when that source is confirmed not applicable.
    Use the aware creation time only if all applicable histories are proven
    complete and empty. Return days without activity only for a signal.

    Example: evaluate_inactivity_risk(active=True, is_closed=False,
    checked_at=datetime(2026, 11, 25, tzinfo=UTC), project_timezone=UTC,
    movement=ActivitySource(None, True), comments=ActivitySource(None, True),
    timesheets=None, created_at=datetime(2026, 11, 18, tzinfo=UTC))
    returns InactivityRiskResult(RiskStatus.TRIGGERED, 7).
    """
    checked_day: date = project_day(checked_at, project_timezone)
    is_open: bool | None = task_is_open(active, is_closed)
    if is_open is False:
        return InactivityRiskResult(RiskStatus.NOT_TRIGGERED)

    checked_utc: datetime = checked_at.astimezone(UTC)
    latest_utc: datetime | None = None
    complete: bool = True
    for source in (movement, comments, timesheets):
        if source is None:
            continue
        complete = complete and source.complete
        if source.latest_at is None:
            continue
        if source.latest_at.utcoffset() is None:
            raise ValueError("Activity timestamps must include a UTC offset.")
        event_utc: datetime = source.latest_at.astimezone(UTC)
        if event_utc > checked_utc:
            complete = False
        elif latest_utc is None or event_utc > latest_utc:
            latest_utc = event_utc

    if latest_utc is None:
        if not complete or created_at is None:
            return InactivityRiskResult(RiskStatus.NOT_EVALUATED)
        if created_at.utcoffset() is None:
            raise ValueError("The creation time must include a UTC offset.")
        latest_utc = created_at.astimezone(UTC)
        if latest_utc > checked_utc:
            return InactivityRiskResult(RiskStatus.NOT_EVALUATED)

    days_without_activity: int = (checked_day - latest_utc.astimezone(project_timezone).date()).days
    if days_without_activity < 7:
        return InactivityRiskResult(RiskStatus.NOT_TRIGGERED)
    if is_open is None or not complete:
        return InactivityRiskResult(RiskStatus.NOT_EVALUATED)
    return InactivityRiskResult(RiskStatus.TRIGGERED, days_without_activity)
