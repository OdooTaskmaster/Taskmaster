"""Verify task tools against real Odoo in a disposable database, then roll back."""

# ruff: noqa: T201 -- This shell-only verifier prints check labels, never raw data.

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel


def run(env: Environment, *, expected_database: str) -> int:
    """Check scoped production readers using synthetic data and always roll back.

    Use a fresh administrator Odoo shell on a disposable database with Project,
    mail_tracking and taskmaster installed. Require a database name beginning
    with taskmaster_ as an additional safety guard. Never use a live database
    or a shell with pending work: rollback includes all work in this transaction.
    Return the number of successful checks; raise on the first failed oracle.
    Example: run(env, expected_database="taskmaster_tools_test").
    """
    from odoo.exceptions import AccessError  # noqa: PLC0415
    from odoo.fields import Command  # noqa: PLC0415

    from odoo.addons.taskmaster.adapters.odoo_task_tools import (  # noqa: PLC0415
        OdooTaskSource,
    )
    from odoo.addons.taskmaster.application.task_read_tools import (  # noqa: PLC0415
        History,
        ReadStatus,
        TaskReadTools,
    )

    if not expected_database.startswith("taskmaster_") or env.cr.dbname != expected_database or not env.su:
        message: str = "Use an administrator shell on the explicitly selected disposable database"
        raise ValueError(message)
    if "mail.tracking.value" not in env.registry.models:
        message = "Use a disposable database with mail_tracking installed"
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
        users: BaseModel = env["res.users"].with_context(no_reset_password=True, tracking_disable=True).create([{
            "name": "Synthetic tool verification", "login": "tool-verification-" + token + str(i),
            "company_id": env.company.id, "company_ids": [Command.set(env.company.ids)],
            "group_ids": [Command.set([env.ref("project.group_project_user").id])],
        } for i in range(3)])
        recipient: Environment = env(user=users[0].id, su=False)
        outsider: Environment = env(user=users[2].id, su=False)
        check("recipient is not a system administrator", not recipient.user.has_group("base.group_system"))
        try:
            recipient["mail.tracking.value"].check_access("read")
        except AccessError:
            check("direct technical journal access stays denied", True)
        else:
            check("direct technical journal access stays denied", False)
        project: BaseModel = env["project.project"].with_context(tracking_disable=True).create({
            "name": "Synthetic tool verification", "privacy_visibility": "followers",
            "company_id": env.company.id, "allowed_internal_user_ids": [Command.set(users[:2].ids)],
        })
        stages: BaseModel = env["project.task.type"].create([
            {"name": name, "project_ids": [Command.link(project.id)]} for name in ("Before", "After")
        ])
        deadline: datetime = datetime(2026, 12, 1, 12)
        task: BaseModel = env["project.task"].with_context(tracking_disable=True).create({
            "name": "Synthetic transition", "project_id": project.id, "stage_id": stages[0].id,
            "user_ids": [Command.set(users[:2].ids)], "date_deadline": deadline,
            "description": "<h1>A</h1><table><tr><td>B</td><td>C&nbsp; D</td></tr></table>",
        })
        other_task: BaseModel = env["project.task"].with_context(tracking_disable=True).create({
            "name": "Another task", "project_id": project.id, "stage_id": stages[0].id,
        })
        # End the fixture cycle; tracking-disabled creation otherwise discards
        # the first subsequent write in the same precommit cycle.
        env["project.task"]._track_finalize()
        today: date = datetime.now(UTC).date()

        def tools(actor: Environment = recipient, *, project_id: int = project.id,
                  task_id: int = task.id) -> TaskReadTools:
            return TaskReadTools(OdooTaskSource(actor, project_id=project_id, task_id=task_id, timezone=UTC))

        def read(actor: Environment = recipient, *, task_id: int = task.id,
                 start: date = today, end: date = today) -> tuple[ReadStatus, History | None]:
            return tools(actor, task_id=task_id).read_task_stage_history(start, end)

        def change(record: BaseModel, values: dict[str, object]) -> BaseModel:
            previous: list[int] = env["mail.message"].search([
                ("model", "=", "project.task"), ("res_id", "=", record.id),
            ]).ids
            tracked: BaseModel = record.with_context(tracking_disable=False, mail_notrack=False)
            tracked.write(values)
            tracked._track_finalize()
            return env["mail.message"].search([
                ("model", "=", "project.task"), ("res_id", "=", record.id),
                ("id", "not in", previous), ("message_type", "=", "tracking"),
            ])

        status, card = tools().read_task_card()
        check("card has exact authorized fields and deadline", status == ReadStatus.AVAILABLE
              and card.name == task.name and card.state == task.state
              and card.deadline == deadline.replace(tzinfo=UTC) and card.deadline_known
              and {a.user_id for a in card.assignees or ()} == set(users[:2].ids)
              and card.source == f"/odoo/tasks/{task.id}")
        check("description normalizes headings tables and whitespace", tools().read_task_description()[1].text == "A\nB\nC D")
        task.with_context(tracking_disable=True).write({"description": "<p>" + "x" * 4001 + "</p>"})
        description = tools().read_task_description()[1]
        check("description is limited and marked truncated", description.text == "x" * 4000 and description.truncated)
        task.with_context(tracking_disable=True).write({"description": False})
        check("empty description is confirmed empty", tools().read_task_description()[1].text == "")  # noqa: PLC1901
        check("outsider description stays hidden", tools(outsider).read_task_description() == (ReadStatus.UNAVAILABLE, None))
        check("hidden and nonexistent cards are indistinguishable", tools(outsider).read_task_card()
              == tools(outsider, task_id=2147483647).read_task_card() == (ReadStatus.UNAVAILABLE, None))
        env["project.task"]._track_finalize()
        stage_messages: BaseModel = change(task, {"stage_id": stages[1].id})
        check("real stage write stores tracking", bool(stage_messages.tracking_value_ids.filtered(lambda t: t.field_id.name == "stage_id")))
        state_messages: BaseModel = change(task, {"state": "02_changes_requested"})
        stored_state: BaseModel = state_messages.tracking_value_ids.filtered(lambda t: t.field_id.name == "state")
        check("real state write stores tracking", bool(stored_state))
        change(task, {"date_deadline": datetime.now(UTC).replace(tzinfo=None)})
        change(other_task, {"stage_id": stages[1].id})
        status, history = read()
        check("ordinary member receives two transitions", status == ReadStatus.AVAILABLE and len(history.transitions) == 2)
        check("stage values match original IDs", any((t.kind, t.old, t.new) == ("stage", stages[0].id, stages[1].id) for t in history.transitions))
        check("state labels match stored values", any((t.kind, t.old, t.new)
              == ("state", stored_state.old_value_char, stored_state.new_value_char) for t in history.transitions))
        check("transition dates match project day", all(t.day == today for t in history.transitions))
        check("only selected task and fields returned", {t.source for t in history.transitions}
              == {f"mail.message:{m.id}" for m in stage_messages | state_messages})
        check("coverage is not invented", not history.complete and not history.truncated)
        check("outsider history blocked", read(outsider) == (ReadStatus.UNAVAILABLE, None))
        check("missing and hidden history indistinguishable", read(outsider, task_id=2147483647) == read(outsider))
        check("superuser caller blocked", read(env) == (ReadStatus.UNAVAILABLE, None))
        check("empty period is explicitly incomplete", read(start=today - timedelta(days=2), end=today - timedelta(days=1))[1] == History((), False))
        for label, start, end in [("reverse period rejected", today, today - timedelta(days=1)),
                                  ("wide period rejected", today - timedelta(days=90), today)]:
            try:
                read(start=start, end=end)
            except ValueError:
                check(label, True)
            else:
                check(label, False)
        stage_messages.write({"date": datetime.combine(today - timedelta(days=1), datetime.min.time())})
        history = read()[1]
        check("out of period transition excluded", len(history.transitions) == 1 and history.transitions[0].kind == "state")
        restriction: BaseModel = env["ir.access"].create({
            "name": "Synthetic tool hidden message", "model_id": env["ir.model"]._get_id("mail.message"),
            "operation": "r", "domain": repr([("id", "not in", state_messages.ids)]),
        })
        check("message record restriction applied before elevation", read()[1] == History((), False))
        restriction.unlink()
        restriction = env["ir.access"].create({
            "name": "Synthetic tool hidden stage", "model_id": env["ir.model"]._get_id("project.task.type"),
            "operation": "r", "domain": repr([("id", "!=", stages[0].id)]),
        })
        check("hidden stage IDs excluded", all(t.kind != "stage" for t in read(start=today - timedelta(days=1))[1].transitions))
        restriction.unlink()
        foreign_company: BaseModel = env["res.company"].create({"name": "Synthetic tool company"})
        foreign_project: BaseModel = env["project.project"].with_context(tracking_disable=True).create({
            "name": "Synthetic tool company project", "company_id": foreign_company.id,
            "privacy_visibility": "followers", "allowed_internal_user_ids": [Command.set(users[:2].ids)],
        })
        foreign_task: BaseModel = env["project.task"].with_context(tracking_disable=True).create({
            "name": "Synthetic tool company task", "project_id": foreign_project.id,
        })
        foreign_tools: TaskReadTools = tools(project_id=foreign_project.id, task_id=foreign_task.id)
        check("company restrictions apply to card", foreign_tools.read_task_card() == (ReadStatus.UNAVAILABLE, None))
        check("company restrictions apply to description", foreign_tools.read_task_description() == (ReadStatus.UNAVAILABLE, None))
        check("company restrictions apply to history", foreign_tools.read_task_stage_history(today, today) == (ReadStatus.UNAVAILABLE, None))
        check("task cannot cross selected project", read(task_id=foreign_task.id) == (ReadStatus.UNAVAILABLE, None))
        original_groups: dict[str, str] = {name: env["project.task"]._fields[name].groups for name in ("stage_id", "state", "description")}
        try:
            env["project.task"]._fields["description"].groups = "base.group_system"
            recipient._field_access_memo.clear()
            check("field-group restriction hides description", tools().read_task_description() == (ReadStatus.UNAVAILABLE, None))
            env["project.task"]._fields["stage_id"].groups = "base.group_system"
            recipient._field_access_memo.clear()
            history = read(start=today - timedelta(days=1))[1]
            check("field-group restriction hides stage transitions", len(history.transitions) == 1 and history.transitions[0].kind == "state")
            env["project.task"]._fields["state"].groups = "base.group_system"
            recipient._field_access_memo.clear()
            check("all transition fields hidden means unavailable", read() == (ReadStatus.UNAVAILABLE, None))
        finally:
            for name, groups in original_groups.items():
                env["project.task"]._fields[name].groups = groups
            recipient._field_access_memo.clear()
        project.with_context(tracking_disable=True).write({"allowed_internal_user_ids": [Command.unlink(users[0].id)]})
        check("membership revocation blocks history", read() == (ReadStatus.UNAVAILABLE, None))
        check("membership revocation blocks card", tools().read_task_card() == (ReadStatus.UNAVAILABLE, None))
        check("membership revocation blocks description", tools().read_task_description() == (ReadStatus.UNAVAILABLE, None))
        check("recipient stays unprivileged", not recipient.su and not recipient.user.has_group("base.group_system"))
        print("RESULT", passed, "checks passed")
        return passed
    finally:
        env.cr.rollback()
        print("ROLLBACK complete; no fixture records committed")
