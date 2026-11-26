"""Assemble a project risk summary without ORM, a model call, or delivery."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from enum import StrEnum

from .assignee_risk import AssigneeRiskResult, evaluate_assignee_risk
from .deadline_risk import (
    DeadlineRiskResult,
    DeadlineTaskSnapshot,
    StalledDeadlineRiskResult,
    evaluate_deadline_risk,
    evaluate_stalled_deadline_risk,
)
from .inactivity_risk import ActivitySource, InactivityRiskResult, evaluate_inactivity_risk
from .project_time import deadline_timing, project_day
from .risk_status import RiskStatus, task_is_open

type RiskReason = DeadlineRiskResult | StalledDeadlineRiskResult | AssigneeRiskResult | InactivityRiskResult

_RULES: tuple[str, ...] = (
    DeadlineRiskResult.rule_id, StalledDeadlineRiskResult.rule_id,
    AssigneeRiskResult.rule_id, InactivityRiskResult.rule_id,
)
_UNKNOWN_HISTORY: ActivitySource = ActivitySource(None, False)


class SummaryReadError(Exception):
    """Report an expected source failure without exposing details in the summary.

    A reader may raise SummaryReadError() before or during iteration; the
    builder converts the exception to a failed or partial result.
    """


class SummaryStatus(StrEnum):
    """Distinguish full results with or without rows, partial results and failures.

    Example: SummaryStatus("partial") returns SummaryStatus.PARTIAL.
    """

    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SummaryAssignee:
    """Identify a confirmed assignee with their permitted display name, if known.

    Example: SummaryAssignee(7, "Demo user") returns a display reference.
    """

    user_id: int
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SummaryTask:
    """Carry an authorized task and its normalized, visible rule inputs.

    Supply a positive Odoo task ID, visible name, deadline snapshot, and
    assignments (None if unknown, () if confirmed empty). Unknown histories
    default to incomplete; pass timesheets=None only when not applicable.
    Example: SummaryTask(9, "Demo", DeadlineTaskSnapshot(True, False, None), ())
    returns a snapshot of an open task with no deadline or assignee.
    """

    task_id: int
    name: str | None
    snapshot: DeadlineTaskSnapshot
    assignees: tuple[SummaryAssignee, ...] | None
    movement: ActivitySource = _UNKNOWN_HISTORY
    comments: ActivitySource = _UNKNOWN_HISTORY
    timesheets: ActivitySource | None = _UNKNOWN_HISTORY
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeadlineDeviation:
    """Store a nonnegative calendar-day distance and its direction.

    Example: DeadlineDeviation(overdue=True, days=0) means a deadline that
    expired earlier today; overdue=False and days=0 means due today.
    """

    overdue: bool
    days: int


@dataclass(frozen=True, slots=True)
class SummaryRow:
    """Record display fields, a relative task link and ordered confirmed reasons.

    The first reason determines the group. None with deadline_known=True
    represents no deadline; None with deadline_known=False is unavailable.
    Example: SummaryRow(9, "Demo", (), None, True, None, "/odoo/tasks/9",
    (AssigneeRiskResult(RiskStatus.TRIGGERED),)) returns a row grouped by RR-03.
    """

    task_id: int
    name: str | None
    assignees: tuple[SummaryAssignee, ...] | None
    deadline: date | datetime | None
    deadline_known: bool
    deviation: DeadlineDeviation | None
    task_url: str
    reasons: tuple[RiskReason, ...]


@dataclass(frozen=True, slots=True)
class ProjectRiskSummary:
    """Return the check status, confirmed rows and coverage by rule.

    Example: ProjectRiskSummary(SummaryStatus.EMPTY, (), _RULES, ()) means
    the authorized data was fully checked and no risk was found.
    """

    status: SummaryStatus
    rows: tuple[SummaryRow, ...]
    checked_rules: tuple[str, ...]
    unchecked_rules: tuple[str, ...]

    @property
    def user_message(self) -> str | None:
        """Return a safe Russian message for this result, or None when complete.

        Example: an EMPTY summary's user_message is the no-risk message.
        """
        if self.status is SummaryStatus.EMPTY:
            return "Рисков не найдено"
        if self.status is SummaryStatus.PARTIAL:
            return "Проверка неполная"
        if self.status is SummaryStatus.FAILED:
            return "Не удалось проверить проект"
        return None


def _evaluate_task(task: SummaryTask, checked_at: datetime, project_timezone: tzinfo) -> tuple[RiskReason, ...]:
    """Return all four outcomes for a normalized task at the given project time.

    Example: _evaluate_task(task, datetime(2026, 11, 25, tzinfo=UTC), UTC)
    returns one outcome per rule, in priority order.
    """
    snapshot: DeadlineTaskSnapshot = task.snapshot
    assignee_ids: tuple[int, ...] | None = (
        None if task.assignees is None else tuple(assignee.user_id for assignee in task.assignees)
    )
    return (
        evaluate_deadline_risk(snapshot, checked_at=checked_at, project_timezone=project_timezone),
        evaluate_stalled_deadline_risk(
            snapshot, checked_at=checked_at, project_timezone=project_timezone,
            last_moved_at=task.movement.latest_at, history_complete=task.movement.complete, created_at=task.created_at,
        ),
        evaluate_assignee_risk(active=snapshot.active, is_closed=snapshot.is_closed, assignee_ids=assignee_ids),
        evaluate_inactivity_risk(
            active=snapshot.active, is_closed=snapshot.is_closed, checked_at=checked_at,
            project_timezone=project_timezone, movement=task.movement, comments=task.comments,
            timesheets=task.timesheets, created_at=task.created_at,
        ),
    )


def _row_order(row: SummaryRow, project_timezone: tzinfo) -> tuple[int, bool, int, datetime, int]:
    """Sort a risk row by group, severity on the project calendar, then ID.

    Example: sorting no-deadline RR-03 rows 9 and 4 in UTC puts row 4 first.
    """
    first: RiskReason = row.reasons[0]
    depth: int = 0
    latest: datetime = datetime.max.replace(tzinfo=UTC)
    exact: datetime = latest
    missing: bool = False
    if isinstance(first, DeadlineRiskResult):
        assert first.overdue_days is not None
        depth = -first.overdue_days
    elif isinstance(first, StalledDeadlineRiskResult):
        assert first.days_until_deadline is not None
        depth = first.days_until_deadline
    elif isinstance(first, AssigneeRiskResult):
        deadline: date | datetime | None = row.deadline
        missing = deadline is None
        if isinstance(deadline, datetime):
            depth = deadline.astimezone(project_timezone).toordinal()
            exact = deadline.astimezone(UTC)
        elif deadline is not None:
            depth = deadline.toordinal()
    else:
        assert first.days_without_activity is not None
        depth = -first.days_without_activity
    return _RULES.index(first.rule_id), missing, depth, exact, row.task_id


def build_project_risk_summary(
    read_tasks: Callable[[], Iterable[SummaryTask]], *, checked_at: datetime, project_timezone: tzinfo,
) -> ProjectRiskSummary:
    """Build a full sorted summary from authorized snapshots of one project.

    The server-side reader must enforce recipient access and normalize values.
    Yield one snapshot per task ID (repeats are ignored) and raise
    SummaryReadError for unavailable or denied reads, not an empty iterable.
    Pass an aware check instant and the project's time zone. Return confirmed
    rows and project-wide coverage; a mid-read failure keeps existing rows but
    leaves all rules unverified for the whole project.

    Example: build_project_risk_summary(lambda: (), checked_at=datetime(2026,
    11, 25, tzinfo=UTC), project_timezone=UTC) returns an EMPTY summary.
    """
    checked_day: date = project_day(checked_at, project_timezone)
    seen: set[int] = set()
    unchecked: set[str] = set()
    rows: list[SummaryRow] = []
    try:
        for task in read_tasks():
            if task.task_id in seen:
                continue
            seen.add(task.task_id)
            snapshot: DeadlineTaskSnapshot = task.snapshot
            is_open: bool | None = task_is_open(snapshot.active, snapshot.is_closed)
            if is_open is not True:
                if is_open is None:
                    unchecked.update(_RULES)
                continue

            outcomes: tuple[RiskReason, ...] = _evaluate_task(task, checked_at, project_timezone)
            unchecked.update(outcome.rule_id for outcome in outcomes if outcome.status is RiskStatus.NOT_EVALUATED)
            reasons: tuple[RiskReason, ...] = tuple(outcome for outcome in outcomes if outcome.status is RiskStatus.TRIGGERED)
            if not reasons:
                continue
            deadline: date | datetime | None = snapshot.deadline if snapshot.deadline_known else None
            deviation: DeadlineDeviation | None = None
            if deadline is not None:
                expired: bool
                offset: int
                expired, offset = deadline_timing(deadline, checked_at, project_timezone, checked_day)
                deviation = DeadlineDeviation(expired, abs(offset))
            rows.append(SummaryRow(
                task.task_id, task.name, task.assignees, deadline, snapshot.deadline_known, deviation,
                f"/odoo/tasks/{task.task_id}", reasons,
            ))
    except SummaryReadError:
        if not seen:
            return ProjectRiskSummary(SummaryStatus.FAILED, (), (), _RULES)
        unchecked.update(_RULES)

    rows.sort(key=lambda row: _row_order(row, project_timezone))
    status: SummaryStatus = SummaryStatus.PARTIAL if unchecked else (SummaryStatus.COMPLETE if rows else SummaryStatus.EMPTY)
    return ProjectRiskSummary(
        status, tuple(rows), tuple(rule for rule in _RULES if rule not in unchecked),
        tuple(rule for rule in _RULES if rule in unchecked),
    )
