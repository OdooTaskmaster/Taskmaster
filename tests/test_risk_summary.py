"""Verify summary assembly, ordering, and read outcomes without live services."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from application.assignee_risk import AssigneeRiskResult
from application.deadline_risk import DeadlineRiskResult, DeadlineTaskSnapshot
from application.inactivity_risk import ActivitySource, InactivityRiskResult
from application.risk_status import RiskStatus
from application.risk_summary import (
    DeadlineDeviation,
    ProjectRiskSummary,
    SummaryAssignee,
    SummaryReadError,
    SummaryRow,
    SummaryStatus,
    SummaryTask,
    build_project_risk_summary,
)

_NOW: datetime = datetime(2026, 11, 25, 6, tzinfo=UTC)
_ZONE: timezone = timezone(timedelta(hours=3))
_ASSIGNEES: tuple[SummaryAssignee, ...] = (SummaryAssignee(7, "Demo user"),)
_RULES: tuple[str, ...] = ("RR-01", "RR-02", "RR-03", "RR-04")


def _task(
    task_id: int, deadline: date | datetime | None = None, *, age: int = 0,
    assignees: tuple[SummaryAssignee, ...] | None = _ASSIGNEES,
    active: bool | None = True, is_closed: bool | None = False, deadline_known: bool = True,
) -> SummaryTask:
    """Return a synthetic snapshot with the supplied task state and age in days.

    The fixture has complete empty histories. Example: _task(1, age=7)
    returns a task created seven days ago with no deadline.
    """
    empty: ActivitySource = ActivitySource(None, True)
    return SummaryTask(
        task_id, f"Task {task_id}", DeadlineTaskSnapshot(active, is_closed, deadline, deadline_known), assignees,
        movement=empty, comments=empty, timesheets=None, created_at=_NOW - timedelta(days=age),
    )


def test_full_summary_sorts_all_groups_and_merges_reasons() -> None:
    """Assert the full synthetic order, display fields and confirmed reasons.

    Example: test_full_summary_sorts_all_groups_and_merges_reasons() returns
    None when the expected rows and no others are produced.
    """
    mixed: SummaryTask = _task(4, date(2026, 11, 20), age=8, assignees=())
    tasks: tuple[SummaryTask, ...] = (
        _task(30, age=8), _task(23, assignees=()), _task(11, date(2026, 11, 28), age=2),
        _task(8, date(2026, 11, 24)), mixed, _task(21, date(2026, 12, 1), assignees=()),
        _task(6, date(2026, 11, 24)), _task(12, date(2026, 11, 26), age=2),
        _task(32, date(2026, 12, 2), age=12), mixed, _task(25, date(2026, 11, 30), assignees=()),
        _task(1, date(2026, 11, 19), age=9, is_closed=True),
        _task(2, date(2026, 11, 19), age=9, active=False), _task(3),
    )
    summary: ProjectRiskSummary = build_project_risk_summary(lambda: tasks, checked_at=_NOW, project_timezone=_ZONE)

    assert summary.status is SummaryStatus.COMPLETE
    assert summary.user_message is None
    assert summary.checked_rules == _RULES and summary.unchecked_rules == ()
    assert [row.task_id for row in summary.rows] == [4, 6, 8, 12, 11, 25, 21, 23, 32, 30]
    first: SummaryRow = summary.rows[0]
    assert (first.name, first.assignees, first.deadline, first.task_url) == (
        "Task 4", (), date(2026, 11, 20), "/odoo/project.task/4",
    )
    assert first.reasons == (
        DeadlineRiskResult(RiskStatus.TRIGGERED, 5), AssigneeRiskResult(RiskStatus.TRIGGERED),
        InactivityRiskResult(RiskStatus.TRIGGERED, 8),
    )
    assert first.deviation == DeadlineDeviation(True, 5)
    assert summary.rows[1].assignees == _ASSIGNEES
    assert summary.rows[3].deviation == DeadlineDeviation(False, 1)
    assert summary.rows[7].deadline is None and summary.rows[7].deadline_known
    assert summary.rows[7].deviation is None
    assert summary.rows[8].deviation == DeadlineDeviation(False, 7)


def test_assignee_group_orders_precise_deadlines_within_a_local_date() -> None:
    """Assert exact times sort chronologically and a date-only deadline lasts longer.

    Example: test_assignee_group_orders_precise_deadlines_within_a_local_date()
    returns None when the earlier exact time appears first.
    """
    tasks: tuple[SummaryTask, ...] = (
        _task(1, date(2026, 11, 26), assignees=()),
        _task(2, datetime(2026, 11, 26, 12, tzinfo=UTC), assignees=()),
        _task(3, datetime(2026, 11, 25, 22, tzinfo=UTC), assignees=()),
    )
    summary: ProjectRiskSummary = build_project_risk_summary(lambda: tasks, checked_at=_NOW, project_timezone=_ZONE)
    assert [row.task_id for row in summary.rows] == [3, 2, 1]


@pytest.mark.parametrize(("deadline", "overdue"), [(_NOW - timedelta(seconds=1), True), (_NOW, False)])
def test_zero_day_deviation_preserves_direction(deadline: datetime, overdue: bool) -> None:
    """Assert a zero day count retains the expected deadline direction.

    Example: test_zero_day_deviation_preserves_direction(_NOW, False)
    returns None when the deadline is not yet overdue.
    """
    summary: ProjectRiskSummary = build_project_risk_summary(
        lambda: (_task(1, deadline, assignees=()),), checked_at=_NOW, project_timezone=_ZONE,
    )
    assert summary.rows[0].deviation == DeadlineDeviation(overdue, 0)


@pytest.mark.parametrize("tasks", [(), (_task(1), _task(2, is_closed=True))])
def test_empty_after_full_check(tasks: tuple[SummaryTask, ...]) -> None:
    """Assert the supplied complete and risk-free project yields EMPTY.

    Example: test_empty_after_full_check(()) returns None when all rules
    are listed as checked even for a project with no tasks.
    """
    summary: ProjectRiskSummary = build_project_risk_summary(lambda: tasks, checked_at=_NOW, project_timezone=_ZONE)
    assert summary == ProjectRiskSummary(SummaryStatus.EMPTY, (), _RULES, ())
    assert summary.user_message == "Рисков не найдено"


def test_partial_history_keeps_overdue_signal() -> None:
    """Assert only the confirmed deadline signal survives missing history.

    Example: test_partial_history_keeps_overdue_signal() returns None
    when RR-04 remains unverified and RR-01 is retained.
    """
    task: SummaryTask = SummaryTask(1, "Demo", DeadlineTaskSnapshot(True, False, date(2026, 11, 24)), _ASSIGNEES)
    summary: ProjectRiskSummary = build_project_risk_summary(lambda: (task,), checked_at=_NOW, project_timezone=_ZONE)
    assert summary.status is SummaryStatus.PARTIAL
    assert summary.user_message == "Проверка неполная"
    assert summary.checked_rules == _RULES[:3] and summary.unchecked_rules == ("RR-04",)
    assert summary.rows[0].reasons == (DeadlineRiskResult(RiskStatus.TRIGGERED, 1),)


def test_unknown_deadline_is_not_exposed_or_used_for_deviation() -> None:
    """Assert that an unavailable deadline never becomes a date or a metric.

    Example: test_unknown_deadline_is_not_exposed_or_used_for_deviation()
    returns None when the assignment signal still appears.
    """
    task: SummaryTask = _task(1, date(2026, 11, 24), deadline_known=False, assignees=(), age=4)
    summary: ProjectRiskSummary = build_project_risk_summary(lambda: (task,), checked_at=_NOW, project_timezone=_ZONE)
    assert summary.status is SummaryStatus.PARTIAL
    assert summary.unchecked_rules == ("RR-01", "RR-02")
    assert not summary.rows[0].deadline_known
    assert summary.rows[0].deadline is None and summary.rows[0].deviation is None
    assert summary.rows[0].reasons == (AssigneeRiskResult(RiskStatus.TRIGGERED),)


def test_unknown_state_has_no_row_and_is_not_a_successful_empty_check() -> None:
    """Assert an unverified task state is not included or treated as safe.

    Example: test_unknown_state_has_no_row_and_is_not_a_successful_empty_check()
    returns None when the outcome is partial without rows.
    """
    summary: ProjectRiskSummary = build_project_risk_summary(
        lambda: (_task(1, date(2026, 11, 24), is_closed=None),), checked_at=_NOW, project_timezone=_ZONE,
    )
    assert summary == ProjectRiskSummary(SummaryStatus.PARTIAL, (), (), _RULES)


@pytest.mark.parametrize("after_task", [False, True])
def test_expected_read_failure_is_sanitized(after_task: bool) -> None:
    """Assert the output of a read failure before or after a confirmed task.

    Example: test_expected_read_failure_is_sanitized(True) returns None when
    the summary keeps the confirmed row and suppresses source details.
    """
    def read_tasks() -> Iterator[SummaryTask]:
        """Yield the fixture if requested, then fail; list(read_tasks()) raises SummaryReadError."""
        if after_task:
            yield _task(1, date(2026, 11, 24))
        message: str = "private source details"
        raise SummaryReadError(message)

    summary: ProjectRiskSummary = build_project_risk_summary(read_tasks, checked_at=_NOW, project_timezone=_ZONE)
    assert summary.status is (SummaryStatus.PARTIAL if after_task else SummaryStatus.FAILED)
    assert summary.user_message == ("Проверка неполная" if after_task else "Не удалось проверить проект")
    assert summary.checked_rules == () and summary.unchecked_rules == _RULES
    assert [row.task_id for row in summary.rows] == ([1] if after_task else [])
    assert "private source details" not in repr(summary)
