"""Read fixed comment and timesheet projections under ordinary recipient rights."""

from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from .odoo_task_tools import _utc_midnight

COMMENT_MESSAGE_TYPES: tuple[str, ...] = ("comment", "email", "email_outgoing")

if __package__ and "." in __package__:
    from ..application.task_read_tools import (  # noqa: TID252
        WORKLOG_LIMIT,
        Comment,
        Comments,
        ReadStatus,
        Worklog,
        WorklogEntry,
        WorklogTotal,
    )
else:
    from application.task_read_tools import (
        WORKLOG_LIMIT,
        Comment,
        Comments,
        ReadStatus,
        Worklog,
        WorklogEntry,
        WorklogTotal,
    )

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel


def _readable(model: "BaseModel", fields: tuple[str, ...]) -> bool:
    """Require every projected field under recipient rights; e.g. _readable(model, ('id',))."""
    return set(fields).issubset(model.fields_get(allfields=fields, attributes=("type",)))


def _comments(
    env: "Environment", task_id: int, zone: tzinfo, start: date, end: date, limit: int,
) -> tuple[ReadStatus, Comments | None]:
    """Read bounded comment HTML for an already authorized task; e.g. _comments(env, 9, UTC, start, end, 5).

    Selection is latest-first by date then ID and covers chatter notes and email
    conversation (comment, email, email_outgoing). Notifications, tracking events,
    attachments and author metadata are not projected. No privileged reads.
    """
    if "mail.message" not in env.registry.models:
        return ReadStatus.UNAVAILABLE, None
    try:
        messages: BaseModel = env["mail.message"]
        fields: tuple[str, ...] = ("id", "date", "body")
        if not _readable(messages, fields):
            return ReadStatus.UNAVAILABLE, None
        rows: list[dict[str, object]] = messages.search_read(
            [("model", "=", "project.task"), ("res_id", "=", task_id),
             ("message_type", "in", list(COMMENT_MESSAGE_TYPES)),
             ("date", ">=", _utc_midnight(start, zone)),
             ("date", "<", _utc_midnight(end + timedelta(days=1), zone))],
            fields=list(fields), order="date desc, id desc", load=None, limit=limit + 1,
        )
        fragments: list[Comment] = []
        for row in rows[:limit]:
            mid: object = row.get("id")
            raw: object = row.get("date")
            body: object = row.get("body")
            if body is False:
                body = ""
            if type(mid) is not int or mid <= 0 or not isinstance(body, str):
                return ReadStatus.FAILED, None
            instant: datetime = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=UTC)
            day: date = instant.astimezone(zone).date()
            if not start <= day <= end:
                return ReadStatus.FAILED, None
            fragments.append(Comment(day, body, f"/mail/message/{mid}"))
        return ReadStatus.AVAILABLE, Comments(tuple(fragments), False, len(rows) > limit)
    except Exception:  # noqa: BLE001
        return ReadStatus.FAILED, None


def _date(raw: object) -> date:
    """Parse a precise work date or fail; e.g. _date('2026-01-01')."""
    if type(raw) is date:
        return raw
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    message: str = "Work date required"
    raise ValueError(message)


def _relation(raw: object) -> int:
    """Read a load=None positive relation ID; e.g. _relation(7) -> 7."""
    if type(raw) is not int or raw <= 0:
        message: str = "Visible relation required"
        raise ValueError(message)
    return raw


def _worklog(
    env: "Environment", project_id: int, task_id: int, start: date, end: date,
) -> tuple[ReadStatus, Worklog | None]:
    """Sum authorized timesheets by actual unit, aggregated per day; e.g. _worklog(env, 4, 9, start, end).

    Read no employees, descriptions, costs or hidden counts. Visible rows are read
    most-recent first; when capped they still report only returned evidence. Empty
    visible sums use the project's company time unit but do not prove zero work.
    Different stored units are never added together or silently converted. The
    default unit is resolved only for an empty selection, so a cleared project time
    unit on a project that has valid rows is never a reason to fail the read.
    """
    if "account.analytic.line" not in env.registry.models:
        return ReadStatus.UNAVAILABLE, None
    try:
        lines: BaseModel = env["account.analytic.line"]
        fields: tuple[str, ...] = ("id", "date", "unit_amount", "product_uom_id")
        # These link fields distinguish actual timesheets from generic analytics.
        if not _readable(lines, (*fields, "task_id", "project_id")):
            return ReadStatus.UNAVAILABLE, None
        projects: BaseModel = env["project.project"]
        if not _readable(projects, ("allow_timesheets", "company_id")):
            return ReadStatus.UNAVAILABLE, None
        project_rows: list[dict[str, object]] = projects.search_read(
            [("id", "=", project_id)], fields=["allow_timesheets", "company_id"], load=None, limit=1,
        )
        if not project_rows or project_rows[0].get("allow_timesheets") is not True:
            return ReadStatus.UNAVAILABLE, None
        rows: list[dict[str, object]] = lines.search_read(
            [("project_id", "=", project_id), ("task_id", "=", task_id),
             ("date", ">=", start), ("date", "<=", end)],
            fields=list(fields), order="date desc, id desc", load=None, limit=WORKLOG_LIMIT + 1,
        )
        truncated: bool = len(rows) > WORKLOG_LIMIT
        by_day: dict[tuple[date, int], tuple[Decimal, int, list[str]]] = {}
        for row in rows[:WORKLOG_LIMIT]:
            rid: int = _relation(row.get("id"))
            unit: int = _relation(row.get("product_uom_id"))
            day: date = _date(row.get("date"))
            raw_amount: object = row.get("unit_amount")
            if type(raw_amount) not in (int, float, Decimal) or not start <= day <= end:
                return ReadStatus.FAILED, None
            amount: Decimal = Decimal(str(raw_amount))
            if not amount.is_finite():
                return ReadStatus.FAILED, None
            key: tuple[date, int] = (day, unit)
            day_amount, count, links = by_day.get(key, (Decimal(0), 0, []))
            by_day[key] = (day_amount + amount, count + 1, links + [f"/odoo/account.analytic.line/{rid}"])
        if by_day:
            entries: tuple[WorklogEntry, ...] = tuple(
                WorklogEntry(day, amount, unit, count, tuple(links))
                for (day, unit), (amount, count, links) in sorted(by_day.items())
            )
            sums: dict[int, Decimal] = {}
            for (_, unit), (amount, _count, _links) in by_day.items():
                sums[unit] = sums.get(unit, Decimal(0)) + amount
            unit_ids: set[int] = set(sums)
        else:
            # Empty visible selection: present an explicit zero only in a configured
            # unit; a cleared/absent unit or company is a configuration gap, not a row error.
            company_id_raw: object = project_rows[0].get("company_id")
            if type(company_id_raw) is not int or company_id_raw <= 0:
                return ReadStatus.UNAVAILABLE, None
            companies: BaseModel = env["res.company"]
            if not _readable(companies, ("project_time_mode_id",)):
                return ReadStatus.UNAVAILABLE, None
            company_rows: list[dict[str, object]] = companies.search_read(
                [("id", "=", company_id_raw)], fields=["project_time_mode_id"], load=None, limit=1,
            )
            if not company_rows:
                return ReadStatus.UNAVAILABLE, None
            default_raw: object = company_rows[0].get("project_time_mode_id")
            if type(default_raw) is not int or default_raw <= 0:
                return ReadStatus.UNAVAILABLE, None
            entries = ()
            sums = {default_raw: Decimal(0)}
            unit_ids = {default_raw}
        units: BaseModel = env["uom.uom"]
        if not _readable(units, ("id", "name")):
            return ReadStatus.UNAVAILABLE, None
        unit_rows: list[dict[str, object]] = units.with_context(active_test=False).search_read(
            [("id", "in", sorted(unit_ids))], fields=["id", "name"], order="id", load=None,
        )
        names: dict[int, str] = {
            row["id"]: row["name"] for row in unit_rows
            if type(row.get("id")) is int and isinstance(row.get("name"), str) and row["name"]
        }
        if set(names) != set(unit_ids):
            return ReadStatus.UNAVAILABLE, None
        totals: tuple[WorklogTotal, ...] = tuple(
            WorklogTotal(unit, names[unit], sums[unit]) for unit in sorted(unit_ids)
        )
        return ReadStatus.AVAILABLE, Worklog(totals, entries, False, truncated, start, end)
    except (ValueError, InvalidOperation):
        return ReadStatus.FAILED, None
    except Exception:  # noqa: BLE001
        return ReadStatus.FAILED, None
