"""Verify capability maps in the marked disposable activity database, then roll back."""

# ruff: noqa: T201 -- Print fixed assertion labels only.

from __future__ import annotations

from datetime import UTC
from os import environ
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel


def run(env: Environment, *, expected_database: str) -> int:
    """Check synthetic maps under ordinary ACLs and always roll back fixtures.

    Reuse the activity verifier's marked-database guard. Install project,
    mail_tracking, taskmaster and hr_timesheet first. Example:
    run(env, expected_database='taskmaster_activity_probe_YYYYMMDD').
    Never invoke from a request or a shell with pending non-verification work.
    """
    from odoo.fields import Command  # noqa: PLC0415
    from odoo.tools import config  # noqa: PLC0415

    from odoo.addons.taskmaster.adapters.odoo_task_tools import (  # noqa: PLC0415
        OdooTaskSource,
    )
    from odoo.addons.taskmaster.application.task_read_map import (  # noqa: PLC0415
        ReadScenario,
        ReadTool,
        TaskReadMap,
    )
    from odoo.addons.taskmaster.application.task_read_tools import (  # noqa: PLC0415
        ReadStatus,
    )
    from odoo.addons.taskmaster.scripts.verify_task_activity import (  # noqa: PLC0415
        _validate_target,
    )

    _validate_target(env, expected_database, environ, config.get("dbfilter"))
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
            "name": "Synthetic read-map verifier", "login": "read-map-verifier-" + token + str(index),
            "company_id": env.company.id, "company_ids": [Command.set(env.company.ids)],
            "group_ids": [Command.set(groups)],
        } for index in range(2)])
        recipient: Environment = env(user=users[0].id, su=False)
        outsider: Environment = env(user=users[1].id, su=False)
        project: BaseModel = env["project.project"].with_context(tracking_disable=True).create({
            "name": "Synthetic read-map verifier", "company_id": env.company.id,
            "privacy_visibility": "followers", "allow_timesheets": True,
            "allowed_internal_user_ids": [Command.set(users[:1].ids)],
        })
        task: BaseModel = env["project.task"].with_context(tracking_disable=True).create({
            "name": "Synthetic read-map task", "project_id": project.id,
            "user_ids": [Command.set(users[:1].ids)],
        })

        def source(actor: Environment = recipient, identifier: int = task.id) -> OdooTaskSource:
            return OdooTaskSource(actor, project_id=project.id, task_id=identifier, timezone=UTC)

        for scenario in ReadScenario:
            status, result = TaskReadMap(source(), scenario=scenario).build()
            check("all five types in authorized scenario " + scenario.value,
                  status == ReadStatus.AVAILABLE and result is not None
                  and tuple(branch.tool for branch in result.branches) == tuple(ReadTool))
        baseline = TaskReadMap(source(), scenario=ReadScenario.RISK_ANALYSIS).build()
        hidden: BaseModel = env["mail.message"].create({
            "model": "project.task", "res_id": task.id, "body": "Synthetic hidden map evidence " + token,
            "message_type": "comment", "subtype_id": env.ref("mail.mt_note").id,
        })
        env["ir.access"].create({
            "name": "read-map-verifier-" + token, "model_id": env["ir.model"]._get_id("mail.message"),
            "operation": "r", "domain": repr([("id", "!=", hidden.id)]),
        })
        check("source row is genuinely hidden", not recipient["mail.message"].search([("id", "=", hidden.id)]))
        check("hidden row does not alter map", TaskReadMap(source(), scenario=ReadScenario.RISK_ANALYSIS).build() == baseline)
        field = env["mail.message"]._fields["body"]
        original_groups = field.groups
        try:
            field.groups = "base.group_system"
            recipient._field_access_memo.clear()
            status, tools = source().read_types()
            check("hidden body removes comments only", status == ReadStatus.AVAILABLE
                  and set(tools or ()) == set(ReadTool) - {ReadTool.COMMENTS})
        finally:
            field.groups = original_groups
            recipient._field_access_memo.clear()
        check("field restoration restores map", TaskReadMap(source(), scenario=ReadScenario.RISK_ANALYSIS).build() == baseline)
        project.allow_timesheets = False
        status, tools = source().read_types()
        check("disabled timesheets remove worklog only", status == ReadStatus.AVAILABLE
              and set(tools or ()) == set(ReadTool) - {ReadTool.WORKLOG})
        project.allow_timesheets = True
        status, result = TaskReadMap(source(), scenario=ReadScenario.DRAFT, allowed_tools=frozenset({ReadTool.CARD})).build()
        check("draft server policy narrows branches", status == ReadStatus.AVAILABLE and result is not None
              and tuple(branch.tool for branch in result.branches) == (ReadTool.CARD,))
        for label, actor, identifier in (
            ("outsider", outsider, task.id), ("missing task", recipient, task.id + 999999),
            ("elevated environment", env, task.id),
        ):
            check(label + " has no map", source(actor, identifier).read_types() == (ReadStatus.UNAVAILABLE, None))
        project.allowed_internal_user_ids = [Command.unlink(users[0].id)]
        check("membership revoked removes map", source().read_types() == (ReadStatus.UNAVAILABLE, None))
    finally:
        env.cr.rollback()
    check("no synthetic users remain", env["res.users"].with_context(active_test=False).search_count(
        [("login", "=like", "read-map-verifier-" + token + "%")],
    ) == 0)
    check("no synthetic project remains", env["project.project"].with_context(active_test=False).search_count(
        [("id", "=", project.id)],
    ) == 0)
    check("no synthetic message remains", env["mail.message"].search_count([("id", "=", hidden.id)]) == 0)
    check("no synthetic access rule remains", env["ir.access"].search_count([("name", "=", "read-map-verifier-" + token)]) == 0)
    env.cr.rollback()
    print("PASS rollback completed; checks", passed)
    return passed
