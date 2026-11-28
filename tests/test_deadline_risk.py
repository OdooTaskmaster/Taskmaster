"""Verify RR-01 at fixed times and without Odoo."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from application.deadline_risk import DeadlineRiskResult, DeadlineTaskSnapshot, evaluate_deadline_risk
from application.risk_status import RiskStatus
from scripts.pilot_demo_project import CARDS, Card


_PROJECT_TZ: timezone = timezone(timedelta(hours=3))
_CHECKED_AT: datetime = datetime(2026, 11, 25, 6, tzinfo=UTC)  # 09:00 in the project.
_YESTERDAY: datetime = datetime(2026, 11, 24, 14, tzinfo=UTC)


@pytest.mark.parametrize(
    ("deadline", "checked_at", "expected_days"),
    [
        pytest.param(_YESTERDAY, _CHECKED_AT, 1, id="R01-A-overdue-even-without-activity-input"),
        pytest.param(datetime(2026, 11, 25, 5, tzinfo=UTC), _CHECKED_AT, 0, id="R01-B-expired-today"),
        pytest.param(_CHECKED_AT, _CHECKED_AT, None, id="R01-B-exact-deadline"),
        pytest.param(_CHECKED_AT + timedelta(seconds=1), _CHECKED_AT, None, id="exact-deadline-in-future"),
        pytest.param(date(2026, 11, 25), _CHECKED_AT, None, id="R01-C-date-only-still-valid"),
        pytest.param(date(2026, 11, 25), datetime(2026, 11, 25, 21, tzinfo=UTC), 1, id="date-only-after-local-midnight"),
        pytest.param(datetime(2026, 11, 25, tzinfo=_PROJECT_TZ), _CHECKED_AT, 0, id="exact-midnight-is-not-date-only"),
        pytest.param(datetime(2026, 11, 24, 22, 30, tzinfo=UTC), _CHECKED_AT, 0, id="R01-E-deadline-shifts-local-date"),
        pytest.param(
            datetime(2026, 11, 24, 20, 55, tzinfo=UTC), datetime(2026, 11, 24, 21, 5, tzinfo=UTC), 1,
            id="ten-minutes-cross-local-midnight",
        ),
        pytest.param(datetime(2026, 11, 28, 10, tzinfo=UTC), _CHECKED_AT, None, id="future-deadline"),
        pytest.param(None, _CHECKED_AT, None, id="known-absent-deadline"),
    ],
)
def test_evaluate_deadline(deadline: date | datetime | None, checked_at: datetime, expected_days: int | None) -> None:
    """Match exact and date-only deadlines against the project calendar."""
    status: RiskStatus = (
        RiskStatus.NOT_TRIGGERED if expected_days is None else RiskStatus.TRIGGERED
    )
    result: DeadlineRiskResult = evaluate_deadline_risk(
        DeadlineTaskSnapshot(True, False, deadline), checked_at=checked_at, project_timezone=_PROJECT_TZ,
    )
    assert (result.rule_id, result.status, result.overdue_days) == ("RR-01", status, expected_days)


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        pytest.param(DeadlineTaskSnapshot(True, True, _YESTERDAY), RiskStatus.NOT_TRIGGERED, id="done-or-cancelled"),
        pytest.param(DeadlineTaskSnapshot(False, False, _YESTERDAY), RiskStatus.NOT_TRIGGERED, id="archived"),
        pytest.param(DeadlineTaskSnapshot(False, None, None, False), RiskStatus.NOT_TRIGGERED, id="known-archived"),
        pytest.param(DeadlineTaskSnapshot(None, True, None, False), RiskStatus.NOT_TRIGGERED, id="known-closed"),
        pytest.param(DeadlineTaskSnapshot(None, False, _YESTERDAY), RiskStatus.NOT_EVALUATED, id="unknown-active"),
        pytest.param(DeadlineTaskSnapshot(True, None, _YESTERDAY), RiskStatus.NOT_EVALUATED, id="unknown-closed"),
        pytest.param(DeadlineTaskSnapshot(True, False, None, False), RiskStatus.NOT_EVALUATED, id="unknown-deadline"),
    ],
)
def test_excluded_and_incomplete_tasks(task: DeadlineTaskSnapshot, expected: RiskStatus) -> None:
    """Exclude known closed tasks and distinguish unreadable inputs from empty values."""
    result: DeadlineRiskResult = evaluate_deadline_risk(task, checked_at=_CHECKED_AT, project_timezone=_PROJECT_TZ)
    assert (result.rule_id, result.status, result.overdue_days) == ("RR-01", expected, None)


@pytest.mark.parametrize(
    ("deadline", "checked_at", "message"),
    [
        (_YESTERDAY, _CHECKED_AT.replace(tzinfo=None), "check time"),
        (_YESTERDAY.replace(tzinfo=None), _CHECKED_AT, "exact deadline"),
    ],
)
def test_require_explicit_offsets(deadline: datetime, checked_at: datetime, message: str) -> None:
    """Never silently interpret a naive datetime in the process time zone."""
    with pytest.raises(ValueError, match=message):
        evaluate_deadline_risk(
            DeadlineTaskSnapshot(True, False, deadline), checked_at=checked_at, project_timezone=_PROJECT_TZ,
        )


def test_require_project_timezone() -> None:
    """Do not fall back to the server time zone when the project setting is missing."""
    with pytest.raises(TypeError, match="project time zone"):
        evaluate_deadline_risk(
            DeadlineTaskSnapshot(True, False, _YESTERDAY),
            checked_at=_CHECKED_AT,
            project_timezone=None,  # type: ignore[arg-type]
        )


def test_retrieval_card_from_demo_fixture() -> None:
    """Check RR-01 using the synthetic retrieval card on the seed day."""
    card: Card = next(card for card in CARDS if card.key == "retrieval")
    due_days: int | None = card.due_days
    assert card.age_days == 0 and due_days == -2
    deadline: datetime = datetime(2026, 11, 25, 17, tzinfo=_PROJECT_TZ) + timedelta(days=due_days)
    result: DeadlineRiskResult = evaluate_deadline_risk(
        DeadlineTaskSnapshot(True, card.state in {"1_done", "1_canceled"}, deadline.astimezone(UTC)),
        checked_at=_CHECKED_AT,
        project_timezone=_PROJECT_TZ,
    )
    assert (result.rule_id, result.status, result.overdue_days) == ("RR-01", RiskStatus.TRIGGERED, 2)
