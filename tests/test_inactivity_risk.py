"""Verify RR-04 on a fixed local calendar without Odoo."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from application.inactivity_risk import ActivitySource, InactivityRiskResult, evaluate_inactivity_risk
from application.risk_status import RiskStatus


_ZONE: timezone = timezone(timedelta(hours=3))
_CHECKED: datetime = datetime(2026, 11, 25, 6, tzinfo=UTC)
_SEVEN: datetime = datetime(2026, 11, 18, 20, tzinfo=UTC)
_SIX: datetime = datetime(2026, 11, 18, 22, tzinfo=UTC)
_RECENT: datetime = datetime(2026, 11, 24, 8, tzinfo=UTC)
_EMPTY: ActivitySource = ActivitySource(None, True)
_UNKNOWN: ActivitySource = ActivitySource(None, False)
_OLD: ActivitySource = ActivitySource(_SEVEN, True)


@pytest.mark.parametrize(
    ("movement", "comments", "timesheets", "created_at", "status", "days"),
    [
        pytest.param(_OLD, _EMPTY, _EMPTY, None, RiskStatus.TRIGGERED, 7, id="seven-local-days"),
        pytest.param(ActivitySource(_SIX, True), _EMPTY, None, None, RiskStatus.NOT_TRIGGERED, None, id="six-local-days"),
        pytest.param(_OLD, ActivitySource(_RECENT, True), None, None, RiskStatus.NOT_TRIGGERED, None, id="recent-human-comment"),
        pytest.param(_OLD, _EMPTY, ActivitySource(_RECENT, True), None, RiskStatus.NOT_TRIGGERED, None, id="recent-time-registration"),
        pytest.param(ActivitySource(_RECENT, True), _OLD, _OLD, None, RiskStatus.NOT_TRIGGERED, None, id="recent-movement-is-latest"),
        pytest.param(_UNKNOWN, ActivitySource(_RECENT, False), _UNKNOWN, None, RiskStatus.NOT_TRIGGERED, None, id="known-recent-event-beats-gaps"),
        pytest.param(_OLD, _UNKNOWN, None, _SEVEN, RiskStatus.NOT_EVALUATED, None, id="unconfirmed-comment-source"),
        pytest.param(_OLD, _EMPTY, _UNKNOWN, None, RiskStatus.NOT_EVALUATED, None, id="applicable-unreadable-timesheets"),
        pytest.param(_OLD, _EMPTY, None, None, RiskStatus.TRIGGERED, 7, id="timesheets-confirmed-not-applicable"),
        pytest.param(_EMPTY, _EMPTY, None, _SEVEN, RiskStatus.TRIGGERED, 7, id="complete-empty-history-counts-from-creation"),
        pytest.param(_EMPTY, _EMPTY, None, _SIX, RiskStatus.NOT_TRIGGERED, None, id="recent-creation-with-complete-history"),
        pytest.param(_UNKNOWN, _EMPTY, None, _SEVEN, RiskStatus.NOT_EVALUATED, None, id="no-creation-fallback-with-gaps"),
        pytest.param(_EMPTY, _EMPTY, None, None, RiskStatus.NOT_EVALUATED, None, id="unknown-creation"),
        pytest.param(_OLD, ActivitySource(_CHECKED + timedelta(seconds=1), True), None, None, RiskStatus.NOT_EVALUATED, None, id="event-after-check"),
    ],
)
def test_activity_sources(
    movement: ActivitySource,
    comments: ActivitySource,
    timesheets: ActivitySource | None,
    created_at: datetime | None,
    status: RiskStatus,
    days: int | None,
) -> None:
    """Assert the expected RR-04 status and day count for the given sources.

    Example: test_activity_sources(_OLD, _EMPTY, None, None,
    RiskStatus.TRIGGERED, 7) returns None on success.
    """
    result: InactivityRiskResult = evaluate_inactivity_risk(
        active=True, is_closed=False, checked_at=_CHECKED, project_timezone=_ZONE,
        movement=movement, comments=comments, timesheets=timesheets, created_at=created_at,
    )
    assert result.rule_id == "RR-04"
    assert result == InactivityRiskResult(status, days)


@pytest.mark.parametrize(
    ("active", "is_closed", "status"),
    [(False, False, RiskStatus.NOT_TRIGGERED), (True, True, RiskStatus.NOT_TRIGGERED),
     (True, None, RiskStatus.NOT_EVALUATED)],
)
def test_task_state(active: bool | None, is_closed: bool | None, status: RiskStatus) -> None:
    """Assert the outcome for task flags despite old, complete activity data.

    Example: test_task_state(True, True, RiskStatus.NOT_TRIGGERED) returns
    None on success.
    """
    result: InactivityRiskResult = evaluate_inactivity_risk(
        active=active, is_closed=is_closed, checked_at=_CHECKED, project_timezone=_ZONE,
        movement=_OLD, comments=_EMPTY, timesheets=None,
    )
    assert result == InactivityRiskResult(status)


def test_require_aware_event_time() -> None:
    """Reject an event without an offset; test_require_aware_event_time() returns None on success."""
    with pytest.raises(ValueError, match="Activity timestamps"):
        evaluate_inactivity_risk(
            active=True, is_closed=False, checked_at=_CHECKED, project_timezone=_ZONE,
            movement=ActivitySource(_SEVEN.replace(tzinfo=None), True), comments=_EMPTY, timesheets=None,
        )
