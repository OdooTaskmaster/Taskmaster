"""Verify the deadline window and evidence of movement for RR-02."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from application.deadline_risk import DeadlineTaskSnapshot, StalledDeadlineRiskResult, evaluate_stalled_deadline_risk
from application.risk_status import RiskStatus


_PROJECT_TZ: timezone = timezone(timedelta(hours=3))
_CHECKED_AT: datetime = datetime(2026, 11, 25, 6, tzinfo=UTC)
_TWO_DAYS_AGO: datetime = datetime(2026, 11, 23, 14, tzinfo=UTC)
_YESTERDAY: datetime = datetime(2026, 11, 23, 22, tzinfo=UTC)  # 24.11 in the project.
_TOMORROW: date = date(2026, 11, 26)
_OPEN_TASK: DeadlineTaskSnapshot = DeadlineTaskSnapshot(True, False, _TOMORROW)


@pytest.mark.parametrize(
    ("deadline", "days_left"),
    [
        pytest.param(date(2026, 11, 28), 3, id="inclusive-three-days"),
        pytest.param(date(2026, 11, 25), 0, id="date-only-today"),
        pytest.param(_CHECKED_AT, 0, id="exact-deadline-now"),
        pytest.param(datetime(2026, 11, 28, 22, tzinfo=UTC), None, id="four-local-days"),
        pytest.param(_CHECKED_AT - timedelta(seconds=1), None, id="expired-today"),
        pytest.param(date(2026, 11, 24), None, id="expired-date-only"),
        pytest.param(None, None, id="confirmed-no-deadline"),
    ],
)
def test_deadline_window(deadline: date | datetime | None, days_left: int | None) -> None:
    """Assert the outcome for a deadline and its expected local day count.

    Example: test_deadline_window(date(2026, 11, 28), 3) completes without error.
    """
    result: StalledDeadlineRiskResult = evaluate_stalled_deadline_risk(
        DeadlineTaskSnapshot(True, False, deadline),
        checked_at=_CHECKED_AT, project_timezone=_PROJECT_TZ,
        last_moved_at=_TWO_DAYS_AGO, history_complete=True,
    )
    expected: StalledDeadlineRiskResult = (
        StalledDeadlineRiskResult(RiskStatus.NOT_TRIGGERED) if days_left is None
        else StalledDeadlineRiskResult(RiskStatus.TRIGGERED, days_left, 2)
    )
    assert result.rule_id == "RR-02"
    assert result == expected


@pytest.mark.parametrize(
    ("task", "last_move", "complete", "created_at", "expected"),
    [
        pytest.param(_OPEN_TASK, _YESTERDAY, True, None, RiskStatus.NOT_TRIGGERED, id="recent-status-change"),
        pytest.param(_OPEN_TASK, _YESTERDAY, False, None, RiskStatus.NOT_TRIGGERED, id="recent-move-with-partial-history"),
        pytest.param(_OPEN_TASK, _TWO_DAYS_AGO, False, None, RiskStatus.NOT_EVALUATED, id="older-move-with-partial-history"),
        pytest.param(_OPEN_TASK, None, False, _TWO_DAYS_AGO, RiskStatus.NOT_EVALUATED, id="unreadable-history"),
        pytest.param(_OPEN_TASK, None, True, _TWO_DAYS_AGO, RiskStatus.TRIGGERED, id="complete-empty-history-counts-from-creation"),
        pytest.param(_OPEN_TASK, None, True, _YESTERDAY, RiskStatus.NOT_TRIGGERED, id="recent-creation"),
        pytest.param(_OPEN_TASK, None, True, None, RiskStatus.NOT_EVALUATED, id="missing-creation"),
        pytest.param(_OPEN_TASK, _CHECKED_AT + timedelta(seconds=1), True, None, RiskStatus.NOT_EVALUATED, id="movement-after-check"),
        pytest.param(DeadlineTaskSnapshot(True, False, None), None, False, None, RiskStatus.NOT_TRIGGERED, id="no-deadline-with-incomplete-history"),
        pytest.param(DeadlineTaskSnapshot(True, False, date(2026, 11, 29)), None, False, None, RiskStatus.NOT_TRIGGERED, id="outside-window-with-incomplete-history"),
        pytest.param(DeadlineTaskSnapshot(True, True, _TOMORROW), None, False, None, RiskStatus.NOT_TRIGGERED, id="closed-task"),
        pytest.param(DeadlineTaskSnapshot(False, False, _TOMORROW), _TWO_DAYS_AGO, True, None, RiskStatus.NOT_TRIGGERED, id="archived-task"),
        pytest.param(DeadlineTaskSnapshot(True, None, _TOMORROW), _TWO_DAYS_AGO, True, None, RiskStatus.NOT_EVALUATED, id="unknown-task-state"),
        pytest.param(DeadlineTaskSnapshot(True, False, None, False), _TWO_DAYS_AGO, True, None, RiskStatus.NOT_EVALUATED, id="unknown-deadline"),
        pytest.param(DeadlineTaskSnapshot(True, False, None, False), _YESTERDAY, False, None, RiskStatus.NOT_TRIGGERED, id="recent-move-excludes-unknown-deadline"),
    ],
)
def test_movement_history(
    task: DeadlineTaskSnapshot,
    last_move: datetime | None,
    complete: bool,
    created_at: datetime | None,
    expected: RiskStatus,
) -> None:
    """Assert an outcome for a task and the supplied movement or creation evidence.

    Example: test_movement_history(_OPEN_TASK, None, False, None,
    RiskStatus.NOT_EVALUATED) completes without error.
    """
    result: StalledDeadlineRiskResult = evaluate_stalled_deadline_risk(
        task, checked_at=_CHECKED_AT, project_timezone=_PROJECT_TZ,
        last_moved_at=last_move, history_complete=complete, created_at=created_at,
    )
    assert result.rule_id == "RR-02"
    if expected is RiskStatus.TRIGGERED:
        assert result == StalledDeadlineRiskResult(expected, 1, 2)
    else:
        assert result == StalledDeadlineRiskResult(expected)


def test_reject_naive_movement_time() -> None:
    """Reject a movement with no offset; test_reject_naive_movement_time() returns None on success."""
    with pytest.raises(ValueError, match="movement or creation time"):
        evaluate_stalled_deadline_risk(
            _OPEN_TASK, checked_at=_CHECKED_AT, project_timezone=_PROJECT_TZ,
            last_moved_at=_TWO_DAYS_AGO.replace(tzinfo=None), history_complete=True,
        )
