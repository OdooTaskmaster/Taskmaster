"""Bind an authorized saved summary to scoped Odoo reads and a fresh release gate."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, tzinfo
from enum import StrEnum
from typing import TYPE_CHECKING

from ._core import summary_store as summaries
from .odoo_background import _recipient_environment
from .odoo_summary_access import can_read_summary
from .odoo_task_tools import OdooTaskSource

if __package__ and "." in __package__:
    from ..application.background_operations import ExecutionDenied  # noqa: TID252
    from ..application.project_time import DEFAULT_PROJECT_TIMEZONE, project_zone  # noqa: TID252
    from ..application.risk_summary import SummaryRow  # noqa: TID252
    from ..application.task_read_map import ReadScenario, ReadTool, TaskReadMap  # noqa: TID252
    from ..application.task_read_plan import ReadBudget, ReadStep, TaskReadRun  # noqa: TID252
    from ..application.task_read_tools import (  # noqa: TID252
        Comments,
        History,
        ReadStatus,
        TaskCard,
        TaskReadTools,
        Worklog,
    )
    from ..application.task_result_release import TaskResultRelease  # noqa: TID252
else:
    from application.background_operations import ExecutionDenied
    from application.project_time import DEFAULT_PROJECT_TIMEZONE, project_zone
    from application.risk_summary import SummaryRow
    from application.task_read_map import ReadScenario, ReadTool, TaskReadMap
    from application.task_read_plan import ReadBudget, ReadStep, TaskReadRun
    from application.task_read_tools import Comments, History, ReadStatus, TaskCard, TaskReadTools, Worklog
    from application.task_result_release import TaskResultRelease

if TYPE_CHECKING:
    from odoo.api import Environment

_INTERNAL_GROUP: str = "base.group_user"


class PrepareStatus(StrEnum):
    """Distinguish a bound analysis from denied scope and an operational failure."""

    READY = "ready"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PreparedRiskAnalysis:
    """Carry one server-bound run, release gate and authorized saved facts.

    Example: prepared = prepare_risk_analysis(env, store, project_id=4,
    task_id=9, result_id='result-1', budget=ReadBudget()); when prepared.status
    is READY, run, gate, snapshot and row are populated.
    """

    status: PrepareStatus
    run: TaskReadRun | None = None
    gate: TaskResultRelease | None = None
    snapshot: summaries.SavedSummary | None = None
    row: SummaryRow | None = None


class _UnavailableSource:
    """Deny every projection after a failed fresh snapshot recheck.

    Example: TaskReadTools(_UnavailableSource()).read_task_card() returns
    (ReadStatus.UNAVAILABLE, None).
    """

    def card(self) -> tuple[ReadStatus, TaskCard | None]:
        """Return no card; card() returns (ReadStatus.UNAVAILABLE, None)."""
        return ReadStatus.UNAVAILABLE, None

    def description(self) -> tuple[ReadStatus, str | None, str | None]:
        """Return no description; description() returns (UNAVAILABLE, None, None)."""
        return ReadStatus.UNAVAILABLE, None, None

    def history(self, start: date, end: date) -> tuple[ReadStatus, History | None]:
        """Return no history; history(start, end) returns (UNAVAILABLE, None)."""
        return ReadStatus.UNAVAILABLE, None

    def comments(self, start: date, end: date, limit: int) -> tuple[ReadStatus, Comments | None]:
        """Return no comments; comments(start, end, limit) returns (UNAVAILABLE, None)."""
        return ReadStatus.UNAVAILABLE, None

    def worklog(self, start: date, end: date) -> tuple[ReadStatus, Worklog | None]:
        """Return no worklog; worklog(start, end) returns (UNAVAILABLE, None)."""
        return ReadStatus.UNAVAILABLE, None


def _is_denied(env: "Environment") -> bool:
    """Return True for an elevated, superuser or non-internal environment.

    Example: _is_denied(env) is True for a portal user without base.group_user.
    """
    from odoo import SUPERUSER_ID  # noqa: PLC0415

    if env.su or env.uid == SUPERUSER_ID:
        return True
    try:
        return not env.user.has_group(_INTERNAL_GROUP)
    except Exception:  # noqa: BLE001
        return True


def _selected_row(saved: summaries.SavedSummary, task_id: int) -> SummaryRow | None:
    """Return the saved row for the selected task, or None when absent.

    Example: _selected_row(saved, 9) returns the SummaryRow for task 9.
    """
    return next((row for row in saved.summary.rows if row.task_id == task_id), None)


def _ordered_companies(env: "Environment") -> tuple[int, ...] | None:
    """Return the caller's ordered allowed company IDs, or None when unavailable.

    Example: _ordered_companies(env) returns (3, 1) in the caller's order.
    """
    try:
        return tuple(env.companies.ids)
    except Exception:  # noqa: BLE001
        return None


def _project_timezone(saved: summaries.SavedSummary) -> tzinfo:
    """Load the recorded project zone or the pilot default without guessing.

    Example: _project_timezone(saved) returns ZoneInfo('Europe/Moscow') when
    the saved summary did not record a zone.
    """
    return project_zone(saved.project_timezone or DEFAULT_PROJECT_TIMEZONE)


def _fresh_tools_factory(
    env: "Environment", store: summaries.SummaryStore, saved: summaries.SavedSummary,
    row: SummaryRow, *, project_id: int, task_id: int, timezone: tzinfo,
    company_ids: tuple[int, ...],
) -> Callable[[], AbstractContextManager[TaskReadTools]]:
    """Build a factory for one fresh non-elevated recipient transaction per check.

    Compare the immutable saved snapshot and the selected row before yielding
    tools, so a changed result or revoked scope yields only unavailable reads.
    Example: gate = TaskResultRelease(_fresh_tools_factory(env, store, saved,
    row, project_id=4, task_id=9, timezone=UTC, company_ids=(3, 1))).
    """
    expected_db: str = env.cr.dbname
    recipient_id: int = saved.recipient_id

    @contextmanager
    def open_fresh() -> Iterator[TaskReadTools]:
        """Open a new transaction, recheck the snapshot, and yield scoped tools."""
        with ExitStack() as stack:
            try:
                recipient: "Environment" = stack.enter_context(
                    _recipient_environment(env, expected_db, recipient_id, company_ids),
                )
            except ExecutionDenied:
                yield TaskReadTools(_UnavailableSource())
                return
            current: summaries.SavedSummary | None = store.get_summary(
                saved.result_id, project_id=project_id, recipient_id=recipient_id,
                authorize=lambda item: can_read_summary(recipient, item),
            )
            if current is None or current != saved or _selected_row(current, task_id) != row:
                yield TaskReadTools(_UnavailableSource())
                return
            yield TaskReadTools(OdooTaskSource(
                recipient, project_id=project_id, task_id=task_id, timezone=timezone,
            ))

    return open_fresh


def prepare_risk_analysis(
    env: "Environment", store: summaries.SummaryStore, *,
    project_id: int, task_id: int, result_id: str, budget: ReadBudget,
) -> PreparedRiskAnalysis:
    """Bind an authorized saved summary, its selected row and one scoped run.

    Require a non-elevated internal recipient, a saved result for the selected
    project and recipient, the selected row and a currently readable card.
    Return READY with the run, release gate, snapshot and row, or UNAVAILABLE
    for denied or missing scope, or FAILED for an invalid or failed binding.
    Do not call a model, the graph, HTTP or the release gate here.

    Example: prepared = prepare_risk_analysis(env, store, project_id=4,
    task_id=9, result_id='result-1', budget=ReadBudget()).
    """
    if (type(project_id) is not int or project_id <= 0 or type(task_id) is not int or task_id <= 0
            or not isinstance(result_id, str) or not result_id.strip()
            or not isinstance(budget, ReadBudget)):
        raise ValueError("Invalid risk analysis scope")

    try:
        if _is_denied(env):
            return PreparedRiskAnalysis(PrepareStatus.UNAVAILABLE)
        recipient_id: int = env.uid
        saved: summaries.SavedSummary | None = store.get_summary(
            result_id, project_id=project_id, recipient_id=recipient_id,
            authorize=lambda item: can_read_summary(env, item),
        )
        if saved is None or saved.project_id != project_id or saved.recipient_id != recipient_id:
            return PreparedRiskAnalysis(PrepareStatus.UNAVAILABLE)
        sources: tuple[int, ...] | None = saved.summary.source_task_ids
        row: SummaryRow | None = _selected_row(saved, task_id)
        if row is None or sources is None or task_id not in sources:
            return PreparedRiskAnalysis(PrepareStatus.UNAVAILABLE)

        timezone: tzinfo = _project_timezone(saved)
        company_ids: tuple[int, ...] | None = _ordered_companies(env)
        if company_ids is None:
            return PreparedRiskAnalysis(PrepareStatus.UNAVAILABLE)
        source: OdooTaskSource = OdooTaskSource(
            env, project_id=project_id, task_id=task_id, timezone=timezone,
        )
        tools: TaskReadTools = TaskReadTools(source)
        status: ReadStatus
        card: TaskCard | None
        status, card = tools.read_task_card()
        if status is ReadStatus.UNAVAILABLE:
            return PreparedRiskAnalysis(PrepareStatus.UNAVAILABLE)
        if status is not ReadStatus.AVAILABLE or card is None:
            return PreparedRiskAnalysis(PrepareStatus.FAILED)

        run: TaskReadRun = TaskReadRun(
            TaskReadMap(source, scenario=ReadScenario.RISK_ANALYSIS), tools,
            budget=budget, initial=((ReadStep(ReadTool.CARD), card),),
        )
        if run.context is None or not run.context.observations:
            return PreparedRiskAnalysis(PrepareStatus.FAILED)

        gate: TaskResultRelease = TaskResultRelease(
            _fresh_tools_factory(
                env, store, saved, row, project_id=project_id, task_id=task_id,
                timezone=timezone, company_ids=company_ids,
            ),
        )
        return PreparedRiskAnalysis(PrepareStatus.READY, run, gate, saved, row)
    except ExecutionDenied:
        return PreparedRiskAnalysis(PrepareStatus.UNAVAILABLE)
    except Exception:  # noqa: BLE001
        return PreparedRiskAnalysis(PrepareStatus.FAILED)
