"""Verify comments and worklogs with real Odoo ACLs in an explicitly disposable database."""

# ruff: noqa: T201 -- Print only assertion labels, never project text or credentials.

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from os import environ
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping

    from odoo.api import Environment
    from odoo.orm.models import BaseModel

DISPOSABLE_MARKER: str = "taskmaster-disposable-activity-verifier-v1"


def _validate_target(env: Environment, expected_database: str, environment: Mapping[str, str], dbfilter: object) -> None:
    """Reject unconfirmed or unmarked targets before fixtures; e.g. _validate_target(env, name, values, None).

    A newly created empty database must be marked with DISPOSABLE_MARKER via
    COMMENT ON DATABASE before installing modules. Never mark a live database or
    a copy of real data. The marker survives installation of module fixtures.
    """
    if (not expected_database.startswith("taskmaster_activity_probe_") or env.cr.dbname != expected_database
            or environment.get("TASKMASTER_VERIFY_DB") != expected_database
            or environment.get("TASKMASTER_VERIFY_CONFIRM") != "DISPOSABLE_ROLLBACK_ONLY"
            or not env.su or dbfilter):
        message: str = "Explicit unrouted administrator shell on a disposable activity database required"
        raise ValueError(message)
    env.cr.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database()")
    marker: tuple[str] | None = env.cr.fetchone()
    if not marker or marker[0] != DISPOSABLE_MARKER:
        message = "Fresh disposable database marker required"
        raise ValueError(message)


def run(env: Environment, *, expected_database: str) -> int:
    """Check ordinary-recipient reads with synthetic fixtures and always roll back.

    Install project, mail_tracking, taskmaster and hr_timesheet in a fresh marked
    database, then call run(env, expected_database='taskmaster_activity_probe_DATE')
    from a clean Odoo shell. No HTTP publication or concurrent final gate is tested.
    Rollback includes all pending work; never call this from an application request.
    """
    from odoo.fields import Command  # noqa: PLC0415
    from odoo.tools import config  # noqa: PLC0415

    from odoo.addons.taskmaster.adapters.odoo_task_tools import (  # noqa: PLC0415
        OdooTaskSource,
    )
    from odoo.addons.taskmaster.application.task_read_tools import (  # noqa: PLC0415
        ReadStatus,
        TaskReadTools,
    )

    _validate_target(env, expected_database, environ, config.get("dbfilter"))
    if "task_id" not in env["account.analytic.line"]._fields:
        message: str = "Install hr_timesheet in the disposable database first"
        raise ValueError(message)
    passed: int = 0

    def check(label: str, condition: bool) -> None:
        nonlocal passed
        if not condition:
            raise AssertionError(label)
        passed += 1
        print("PASS", label)

    try:
        token: str = uuid4().hex
        groups: list[int] = [env.ref("project.group_project_user").id, env.ref("hr_timesheet.group_hr_timesheet_user").id]
        users: BaseModel = env["res.users"].with_context(no_reset_password=True, tracking_disable=True).create([{
            "name": "Synthetic activity verifier", "login": "activity-verifier-" + token + str(index),
            "company_id": env.company.id, "company_ids": [Command.set(env.company.ids)],
            "group_ids": [Command.set(groups)],
        } for index in range(3)])
        employees: BaseModel = env["hr.employee"].create([{
            "name": "Synthetic activity verifier", "user_id": user.id, "company_id": env.company.id,
        } for user in users[:2]])
        recipient: Environment = env(user=users[0].id, su=False)
        outsider: Environment = env(user=users[2].id, su=False)
        check("recipient has no system or timesheet manager role", not recipient.su
              and not recipient.user.has_group("base.group_system")
              and not recipient.user.has_group("hr_timesheet.group_timesheet_manager"))
        project: BaseModel = env["project.project"].with_context(tracking_disable=True).create({
            "name": "Synthetic activity verifier", "privacy_visibility": "followers", "allow_timesheets": True,
            "company_id": env.company.id, "allowed_internal_user_ids": [Command.set(users[:2].ids)],
        })
        task: BaseModel = env["project.task"].with_context(tracking_disable=True).create({
            "name": "Synthetic activity task", "project_id": project.id, "user_ids": [Command.set(users[:2].ids)],
        })
        other: BaseModel = env["project.task"].with_context(tracking_disable=True).create({
            "name": "Other synthetic task", "project_id": project.id,
        })
        today: date = datetime.now(UTC).date()
        yesterday: date = today - timedelta(days=1)
        instant: datetime = datetime.combine(today, datetime.min.time()).replace(hour=12)

        def tools(actor: Environment = recipient, task_id: int = task.id) -> TaskReadTools:
            return TaskReadTools(OdooTaskSource(actor, project_id=project.id, task_id=task_id, timezone=UTC))

        def comment(record: BaseModel, body: str, when: datetime = instant) -> BaseModel:
            message: BaseModel = record.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")
            message.date = when
            return message

        visible: BaseModel = comment(task, "Visible synthetic note")
        hidden: BaseModel = comment(task, "Hidden synthetic note")
        old: BaseModel = comment(task, "Old synthetic note", instant - timedelta(days=1))
        unrelated: BaseModel = comment(other, "Other synthetic note")
        rule: BaseModel = env["ir.access"].create({
            "name": "activity-verifier-" + token, "model_id": env["ir.model"]._get_id("mail.message"),
            "operation": "r", "domain": repr([("id", "!=", hidden.id)]),
        })
        status, comments = tools().read_task_comments(today, today)
        check("only visible selected-task comments in period", status == ReadStatus.AVAILABLE and comments is not None
              and {item.source for item in comments.fragments} == {f"/mail/message/{visible.id}"})
        check("hidden and out-of-scope sources absent", comments is not None and not {
            f"/mail/message/{hidden.id}", f"/mail/message/{old.id}", f"/mail/message/{unrelated.id}",
        }.intersection(item.source for item in comments.fragments))
        check("selected comments do not claim full coverage", comments is not None and not comments.complete)
        check("hidden comment ACL is real", not recipient["mail.message"].search([("id", "=", hidden.id)]))
        long: BaseModel = comment(task, "x" * 2001, instant + timedelta(minutes=1))
        status, comments = tools().read_task_comments(today, today, 1)
        check("count and text caps explicit", status == ReadStatus.AVAILABLE and comments is not None
              and comments.truncated and len(comments.fragments) == 1 and comments.fragments[0].truncated
              and len(comments.fragments[0].text) == 2000 and comments.fragments[0].source == f"/mail/message/{long.id}")

        def line(employee: BaseModel, amount: float, day: date = today, record: BaseModel = task) -> BaseModel:
            return env["account.analytic.line"].create({
                "name": "Synthetic activity evidence", "project_id": project.id, "task_id": record.id,
                "employee_id": employee.id, "date": day, "unit_amount": amount,
            })

        conversation: BaseModel = env["mail.message"].create([{
            "model": "project.task", "res_id": task.id, "body": "Synthetic conversation",
            "message_type": kind, "date": instant,
        } for kind in ("email", "email_outgoing", "notification")])
        status, comments = tools().read_task_comments(today, today)
        selected_sources: set[str] = {item.source for item in comments.fragments} if comments else set()
        check("incoming and outgoing email included", status == ReadStatus.AVAILABLE
              and {f"/mail/message/{message.id}" for message in conversation[:2]} <= selected_sources)
        check("system notification excluded", f"/mail/message/{conversation[2].id}" not in selected_sources)

        first: BaseModel = line(employees[0], 2)
        second: BaseModel = line(employees[0], 1.5)
        hidden_line: BaseModel = line(employees[1], 7)
        old_line: BaseModel = line(employees[0], 50, yesterday)
        other_line: BaseModel = line(employees[0], 80, record=other)
        status, worklog = tools().read_task_worklog(today, today)
        check("known visible sum is exactly 3.5", status == ReadStatus.AVAILABLE and worklog is not None
              and len(worklog.totals) == 1 and worklog.totals[0].amount == Decimal("3.5")
              and (worklog.period_start, worklog.period_end) == (today, today))
        check("unit is actual stored UOM", worklog is not None
              and worklog.totals[0].unit_id == first.product_uom_id.id
              and worklog.totals[0].unit_name == first.product_uom_id.with_env(recipient).name)
        check("sum has exactly visible evidence", worklog is not None and len(worklog.entries) == 1
              and worklog.entries[0].day == today and worklog.entries[0].amount == Decimal("3.5")
              and worklog.entries[0].unit_id == first.product_uom_id.id and worklog.entries[0].count == 2
              and set(worklog.entries[0].sources) == {
                  f"/odoo/account.analytic.line/{first.id}", f"/odoo/account.analytic.line/{second.id}",
              })
        check("hidden analytic ACL is real", not recipient["account.analytic.line"].search([("id", "=", hidden_line.id)]))
        check("hidden old and other-task evidence absent", worklog is not None and not {
            f"/odoo/account.analytic.line/{hidden_line.id}", f"/odoo/account.analytic.line/{old_line.id}",
            f"/odoo/account.analytic.line/{other_line.id}",
        }.intersection(link for entry in worklog.entries for link in entry.sources))
        check("sum never claims full coverage", worklog is not None and not worklog.complete and not worklog.truncated)
        original_unit: BaseModel = env.company.project_time_mode_id
        try:
            env.company.project_time_mode_id = False
            status, configured_worklog = tools().read_task_worklog(today, today)
            check("cleared company unit does not hide valid rows", status == ReadStatus.AVAILABLE
                  and configured_worklog is not None and configured_worklog.totals[0].amount == Decimal("3.5"))
            check("empty selection without company unit unavailable", tools().read_task_worklog(
                today + timedelta(days=1), today + timedelta(days=1),
            ) == (ReadStatus.UNAVAILABLE, None))
        finally:
            env.company.project_time_mode_id = original_unit
        env["account.analytic.line"].create([{
            "name": "Synthetic capped evidence", "project_id": project.id, "task_id": task.id,
            "employee_id": employees[0].id, "date": today, "unit_amount": 1,
        } for _ in range(999)])
        status, worklog = tools().read_task_worklog(today, today)
        check("1001 visible rows give capped partial sum with evidence", status == ReadStatus.AVAILABLE
              and worklog is not None and worklog.truncated and not worklog.complete
              and len(worklog.entries) == 1 and worklog.entries[0].count == 1000
              and worklog.totals[0].amount == Decimal("1000.5"))
        for index in range(20):
            comment(task, "Synthetic capped comment", instant + timedelta(minutes=index + 2))
        status, comments = tools().read_task_comments(today, today, 20)
        check("maximum 20 visible fragments enforced", status == ReadStatus.AVAILABLE and comments is not None
              and len(comments.fragments) == 20 and comments.truncated and not comments.complete)
        for model_name, field_name, method in (
            ("mail.message", "body", "read_task_comments"),
            ("account.analytic.line", "unit_amount", "read_task_worklog"),
        ):
            field = env[model_name]._fields[field_name]
            original_groups = field.groups
            try:
                field.groups = "base.group_system"
                recipient._field_access_memo.clear()
                check("hidden source field denied " + method,
                      getattr(tools(), method)(today, today) == (ReadStatus.UNAVAILABLE, None))
            finally:
                field.groups = original_groups
                recipient._field_access_memo.clear()
        status, worklog = tools().read_task_worklog(today + timedelta(days=1), today + timedelta(days=1))
        check("empty visible sum is not proven zero work", status == ReadStatus.AVAILABLE and worklog is not None
              and not worklog.entries and worklog.totals[0].amount == 0 and not worklog.complete)
        for actor, identifier in ((outsider, task.id), (recipient, task.id + other.id + 999999), (env, task.id)):
            for method in ("read_task_comments", "read_task_worklog"):
                check("hidden missing or elevated scope denied " + method,
                      getattr(tools(actor, identifier), method)(today, today) == (ReadStatus.UNAVAILABLE, None))
        for method in ("read_task_comments", "read_task_worklog"):
            try:
                getattr(tools(), method)(today, yesterday)
            except ValueError:
                check("invalid period rejected " + method, True)
            else:
                check("invalid period rejected " + method, False)
        for limit in (0, 21, True):
            try:
                tools().read_task_comments(today, today, limit)
            except ValueError:
                check("invalid comment limit rejected", True)
            else:
                check("invalid comment limit rejected", False)
        project.allow_timesheets = False
        check("disabled timesheets not zero", tools().read_task_worklog(today, today) == (ReadStatus.UNAVAILABLE, None))
        project.allow_timesheets = True
        rule.domain = repr([("id", "=", 0)])
        status, comments = tools().read_task_comments(today, today)
        check("all comments filtered does not prove empty history", status == ReadStatus.AVAILABLE
              and comments is not None and not comments.fragments and not comments.complete)
        project.allowed_internal_user_ids = [Command.unlink(users[0].id)]
        for method in ("read_task_comments", "read_task_worklog"):
            check("membership rechecked " + method, getattr(tools(), method)(today, today) == (ReadStatus.UNAVAILABLE, None))
        check("recipient remains unprivileged", not recipient.su)
    finally:
        env.cr.rollback()
    print("PASS rollback completed; checks", passed)
    return passed
