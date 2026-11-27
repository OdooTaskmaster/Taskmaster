"""Collect report observations using the requesting user's Odoo environment."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from .odoo_summary_access import can_read_summary
from .odoo_task_reader import readable_projects
from .odoo_task_tools import _utc_midnight

if __package__ and "." in __package__:
    from ..application import report_facts as core
    from ..application.operation_store import StateStoreError
    from ..application.summary_store import SavedSummary, SummaryStore
else:
    from application import report_facts as core
    from application.operation_store import StateStoreError
    from application.summary_store import SavedSummary, SummaryStore

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel

_LIMIT: int = 500
_MAX_SNAPSHOTS: int = 20
type Domain = list[tuple[str, str, object]]
_MISSING: core.SectionFacts = core.SectionFacts(core.FactCoverage.UNAVAILABLE)
_SKIPPED: core.SectionFacts = core.SectionFacts(core.FactCoverage.NOT_APPLICABLE)


def _id(value: object) -> bool:
    """Recognize a positive record ID; _id(True) returns False."""
    return type(value) is int and value > 0


def _instant(value: object) -> datetime:
    """Normalize Odoo datetimes; _instant(datetime(2026, 1, 1)) returns UTC midnight."""
    if isinstance(value, str) and (" " in value or "T" in value):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("An Odoo datetime is required.")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _task_ref(task_id: int) -> str:
    """Build the Odoo task link; _task_ref(9) returns '/odoo/project.task/9'."""
    return f"/odoo/project.task/{task_id}"


def _section(rows: list[core.ReportFact], incomplete: bool) -> core.SectionFacts:
    """Freeze observations; _section([], False) returns a checked empty section."""
    return core.SectionFacts(core.FactCoverage.INCOMPLETE if incomplete else core.FactCoverage.CHECKED, tuple(rows))


def _require_fields(model: "BaseModel", fields: tuple[str, ...]) -> bool:
    """Check visible model metadata; _require_fields(model, ('id',)) returns a bool."""
    return set(fields).issubset(model.fields_get(allfields=fields, attributes=("type",)))


def _safe_read(read: Callable[[], core.SectionFacts]) -> core.SectionFacts:
    """Map expected source failures to a neutral gap; _safe_read(read) returns no raw error."""
    from odoo.exceptions import AccessError, MissingError

    try:
        return read()
    except (AccessError, MissingError, StateStoreError, KeyError, ValueError):
        return _MISSING


def _task_facts(tasks: "BaseModel", domain: Domain, start: datetime, stop: datetime, now: datetime,
                *, current: bool = False) -> core.SectionFacts:
    """Read either period-created tasks or currently open tasks from a scoped domain."""
    fields: tuple[str, ...] = ("id", "create_date", "active", "is_closed", "state") if current else ("id", "create_date")
    if not _require_fields(tasks, fields):
        return _MISSING
    selection: Domain = [*domain, ("create_date", "<=", now.replace(tzinfo=None))]
    if current:
        selection += [("active", "=", True), ("is_closed", "=", False)]
    else:
        selection += [("create_date", ">=", start.replace(tzinfo=None)), ("create_date", "<", stop.replace(tzinfo=None))]
    rows: list[dict[str, object]] = tasks.search_read(selection, fields=list(fields), order="id", load=None, limit=_LIMIT + 1)
    facts: list[core.ReportFact] = []
    gap: bool = len(rows) > _LIMIT
    for row in rows[:_LIMIT]:
        task_id: object = row.get("id")
        if not _id(task_id):
            gap = True
            continue
        try:
            added: datetime = _instant(row.get("create_date"))
        except (ValueError, OverflowError):
            gap = True
            continue
        if current and added <= now and row.get("active") is True and row.get("is_closed") is False:
            state: object = row.get("state")
            if isinstance(state, str) and state:
                facts.append(core.ReportFact(now, task_id, (_task_ref(task_id),), current_state=state))
            else:
                gap = True
        elif not current and start <= added < stop and added <= now:
            facts.append(core.ReportFact(added, task_id, (_task_ref(task_id),)))
    return _section(facts, gap)


def _completion_events(
    env: "Environment", catalog: set[int], start: datetime, stop: datetime, now: datetime,
    catalog_gap: bool,
) -> core.SectionFacts:
    """Read visible task-done tracking; an empty read cannot prove no completion."""
    if not catalog:
        return _section([], catalog_gap)
    subtype = env.ref("project.mt_task_done", raise_if_not_found=False)
    messages: BaseModel = env["mail.message"]
    fields: tuple[str, ...] = ("id", "res_id", "date")
    if not subtype or not _require_fields(messages, (*fields, "model", "subtype_id", "message_type")):
        return _MISSING
    rows: list[dict[str, object]] = messages.search_read(
        [("model", "=", "project.task"), ("res_id", "in", sorted(catalog)), ("subtype_id", "=", subtype.id),
         ("message_type", "=", "tracking"), ("date", ">=", start.replace(tzinfo=None)),
         ("date", "<", stop.replace(tzinfo=None)), ("date", "<=", now.replace(tzinfo=None))],
        fields=list(fields), order="date, id", load=None, limit=_LIMIT + 1,
    )
    facts: list[core.ReportFact] = []
    for row in rows[:_LIMIT]:
        task_id: object = row.get("res_id")
        record_id: object = row.get("id")
        if not _id(task_id) or task_id not in catalog or not _id(record_id):
            continue
        try:
            when: datetime = _instant(row.get("date"))
        except (ValueError, OverflowError):
            continue
        if start <= when < stop and when <= now:
            facts.append(core.ReportFact(when, task_id, (_task_ref(task_id), f"/mail/message/{record_id}")))
    # Tracking can omit events (including combined stage/state changes).
    return _section(facts, True)


def _worklog(
    env: "Environment", project: "BaseModel", project_id: int, catalog: set[int], period: core.ReportPeriod,
    today: date, catalog_gap: bool,
) -> core.SectionFacts:
    """Read visible task-bound timesheets by work day; keep each recorded unit separate."""
    if not catalog:
        return _section([], catalog_gap)
    if not _require_fields(project, ("allow_timesheets",)) or "account.analytic.line" not in env.registry.models:
        return _MISSING
    allowed: object = project.allow_timesheets
    if allowed is False:
        return _SKIPPED
    if allowed is not True:
        return _MISSING
    lines: BaseModel = env["account.analytic.line"]
    fields: tuple[str, ...] = ("id", "task_id", "date", "unit_amount", "product_uom_id")
    if not _require_fields(lines, (*fields, "project_id")):
        return _MISSING
    rows: list[dict[str, object]] = lines.search_read(
        [("project_id", "=", project_id), ("task_id", "in", sorted(catalog)), ("date", ">=", period.start),
         ("date", "<=", min(today, period.end))], fields=list(fields), order="date, id", load=None, limit=_LIMIT + 1,
    )
    unit_ids: set[int] = {row["product_uom_id"] for row in rows[:_LIMIT] if _id(row.get("product_uom_id"))}
    visible_units: set[int] = set()
    if unit_ids:
        units: BaseModel = env["uom.uom"].with_context(active_test=False)
        visible_units = set(units.search([("id", "in", sorted(unit_ids))]).ids)
    facts: list[core.ReportFact] = []
    for row in rows[:_LIMIT]:
        task_id: object = row.get("task_id")
        record_id: object = row.get("id")
        unit_id: object = row.get("product_uom_id")
        day: object = row.get("date")
        amount: object = row.get("unit_amount")
        if (not _id(task_id) or task_id not in catalog or not _id(record_id) or not _id(unit_id)
                or unit_id not in visible_units or type(amount) not in (int, float, Decimal)):
            continue
        try:
            if isinstance(day, str):
                day = date.fromisoformat(day)
            quantity: Decimal = Decimal(str(amount))
        except (ValueError, InvalidOperation):
            continue
        if type(day) is date and period.start <= day <= min(today, period.end) and quantity.is_finite():
            facts.append(core.ReportFact(day, task_id, (_task_ref(task_id), f"/odoo/account.analytic.line/{record_id}"),
                                         quantity=quantity, unit_id=unit_id))
    return _section(facts, True)  # Visible lines do not establish total project activity.


def _risk_facts(
    env: "Environment", project_id: int, store: "SummaryStore | None", result_ids: tuple[str, ...],
    start: datetime, stop: datetime, now: datetime,
) -> core.SectionFacts:
    """Read selected authorized snapshots in the period, never substituting a newer result."""
    if store is None or not result_ids:
        return _MISSING
    found: list[core.ReportFact] = []
    for result_id in result_ids:
        try:
            saved: SavedSummary | None = store.get_summary(result_id, project_id=project_id, recipient_id=env.uid,
                                                          authorize=lambda snap: can_read_summary(env, snap))
        except StateStoreError:
            continue
        if (saved is None or saved.result_id != result_id or saved.project_id != project_id
                or saved.recipient_id != env.uid or saved.checked_at.utcoffset() is None
                or not start <= saved.checked_at < stop or saved.checked_at > now):
            continue
        summary_ref: str = "/taskmaster/risk-summary?" + urlencode({"project_id": project_id, "result_id": result_id})
        for row in saved.summary.rows:
            if len(found) == _LIMIT:
                return _section(found, True)
            found.append(core.ReportFact(saved.checked_at, row.task_id, (_task_ref(row.task_id), summary_ref),
                                         risk_rules=tuple(reason.rule_id for reason in row.reasons)))
    found.sort(key=lambda fact: (fact.when, fact.task_id, fact.sources))
    # A selection cannot establish coverage across the entire report period.
    return _section(found, True)


def read_report_facts(
    env: "Environment", *, project_id: int, period: core.ReportPeriod, project_timezone: tzinfo,
    observed_at: datetime, summary_store: "SummaryStore | None" = None, risk_result_ids: tuple[str, ...] = (),
) -> core.ReportFacts:
    """Collect read-only facts for one authorized project using server-selected inputs.

    Provide aware observation time, project zone and optional saved risk IDs.
    Example: read_report_facts(env, project_id=4, period=period,
    project_timezone=UTC, observed_at=now) returns scoped sections or UNAVAILABLE.
    """
    from odoo import SUPERUSER_ID
    from odoo.exceptions import AccessError, MissingError

    if (not _id(project_id) or not isinstance(period, core.ReportPeriod) or not isinstance(project_timezone, tzinfo)
            or not isinstance(observed_at, datetime) or observed_at.utcoffset() is None
            or type(risk_result_ids) is not tuple or len(risk_result_ids) > _MAX_SNAPSHOTS
            or any(type(key) is not str or not key.isascii() or not 0 < len(key) <= 128 for key in risk_result_ids)):
        raise ValueError("A valid report scope, observation time and bounded selection are required.")
    project: BaseModel | None = None if env.su or env.uid == SUPERUSER_ID else readable_projects(env, project_id)
    if not project:
        return core.assemble_report_facts(period, None)
    start: datetime = _utc_midnight(period.start, project_timezone).replace(tzinfo=UTC)
    stop: datetime = _utc_midnight(period.end + timedelta(days=1), project_timezone).replace(tzinfo=UTC)
    if start >= stop:
        raise ValueError("The report period has no valid local interval.")
    now: datetime = observed_at.astimezone(UTC)
    sections: dict[core.ReportSection, core.SectionFacts] = {}
    if start <= now:
        tasks: BaseModel = env["project.task"].with_context(active_test=False)
        domain: Domain = [("project_id", "=", project_id)]
        sections[core.ReportSection.CREATED] = _safe_read(lambda: _task_facts(tasks, domain, start, stop, now))
        sections[core.ReportSection.CURRENT] = (
            _safe_read(lambda: _task_facts(tasks, domain, start, stop, now, current=True)) if now < stop else _SKIPPED
        )
        try:
            if _require_fields(tasks, ("id",)):
                ids: list[int] = tasks.search(domain, order="id", limit=_LIMIT + 1).ids
                catalog: set[int] = set(ids[:_LIMIT])
                gap: bool = len(ids) > _LIMIT
                sections[core.ReportSection.COMPLETED] = _safe_read(lambda: _completion_events(env, catalog, start, stop, now, gap))
                sections[core.ReportSection.WORKLOG] = _safe_read(
                    lambda: _worklog(env, project, project_id, catalog, period, now.astimezone(project_timezone).date(), gap),
                )
        except (AccessError, MissingError, ValueError):
            pass
    sections[core.ReportSection.RISKS] = _safe_read(
        lambda: _risk_facts(env, project_id, summary_store, tuple(dict.fromkeys(risk_result_ids)), start, stop, now),
    )
    return core.assemble_report_facts(period, sections)
