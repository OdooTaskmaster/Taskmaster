"""Verify a bounded read and committed access revocation in a marked disposable database."""

# ruff: noqa: T201 -- Print synthetic assertion labels, never raw project data.

from __future__ import annotations

from datetime import UTC, datetime
from os import environ
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping

    from odoo.api import Environment
    from odoo.orm.models import BaseModel


def _validate_commit_confirmation(environment: Mapping[str, str]) -> None:
    """Require extra consent for synthetic commits; e.g. _validate_commit_confirmation(environ)."""
    if environment.get("TASKMASTER_VERIFY_COMMIT_CONFIRM") != "DISPOSABLE_SYNTHETIC_COMMIT_AND_CLEANUP":
        message: str = "Explicit confirmation of synthetic commits and cleanup required"
        raise ValueError(message)


def run(env: Environment, *, expected_database: str) -> int:
    """Create, commit, revoke and remove synthetic fixtures only in a marked disposable DB.

    Example: run(env, expected_database='taskmaster_activity_probe_DATE'). Requires
    the activity verifier's exact-name/marker/confirmation guards plus explicit
    TASKMASTER_VERIFY_COMMIT_CONFIRM. Unlike rollback-
    only verifiers, cross-cursor visibility requires synthetic fixture commits;
    cleanup commits their removal in finally. A killed process may leave fixtures.
    All earlier pending shell work is rolled back before creating fixtures.
    """
    from odoo.api import Environment  # noqa: PLC0415
    from odoo.fields import Command  # noqa: PLC0415
    from odoo.tools import config  # noqa: PLC0415

    from odoo.addons.taskmaster.adapters.odoo_source_gate import (  # noqa: PLC0415
        OdooContextVerifier,
    )
    from odoo.addons.taskmaster.adapters.odoo_task_tools import (  # noqa: PLC0415
        OdooTaskSource,
    )
    from odoo.addons.taskmaster.application.reading_plan import (  # noqa: PLC0415
        PlanStop,
        ReadingRun,
        ReadPlan,
        ReadStep,
        SourceRef,
    )
    from odoo.addons.taskmaster.application.source_gate import (  # noqa: PLC0415
        Candidate,
        GateStatus,
        SourceGate,
    )
    from odoo.addons.taskmaster.application.task_read_map import (  # noqa: PLC0415
        ReadScenario,
        ReadTool,
        TaskReadMap,
    )
    from odoo.addons.taskmaster.application.task_read_tools import (  # noqa: PLC0415
        TaskReadTools,
    )
    from odoo.addons.taskmaster.scripts.verify_task_activity import (  # noqa: PLC0415
        _validate_target,
    )

    _validate_target(env, expected_database, environ, config.get("dbfilter"))
    _validate_commit_confirmation(environ)
    env.cr.rollback()
    env.invalidate_all()
    passed: int = 0
    token: str = "reading-gate-" + uuid4().hex
    user_id: int = 0
    project_id: int = 0
    rule_id: int = 0
    partner_id: int = 0

    def check(label: str, condition: bool) -> None:
        nonlocal passed
        if not condition:
            raise AssertionError(label)
        passed += 1
        print("PASS", label)

    try:
        user: BaseModel = env["res.users"].with_context(no_reset_password=True, tracking_disable=True).create({
            "name": token, "login": token, "company_id": env.company.id,
            "company_ids": [Command.set(env.company.ids)],
            "group_ids": [Command.set([env.ref("project.group_project_user").id])],
        })
        user_id = user.id
        partner_id = user.partner_id.id
        project: BaseModel = env["project.project"].with_context(tracking_disable=True).create({
            "name": token, "privacy_visibility": "followers", "company_id": env.company.id,
            "allowed_internal_user_ids": [Command.set([user_id])], "allow_timesheets": False,
        })
        project_id = project.id
        task: BaseModel = env["project.task"].with_context(tracking_disable=True).create({
            "name": token, "description": "Synthetic visible task description", "project_id": project_id,
            "user_ids": [Command.set([user_id])],
        })
        message: BaseModel = task.message_post(body="Synthetic unquoted evidence", message_type="comment", subtype_xmlid="mail.mt_note")
        task_id: int = task.id
        message_id: int = message.id
        env.cr.commit()
        env.invalidate_all()
        recipient: Environment = env(user=user_id, su=False)
        source: OdooTaskSource = OdooTaskSource(recipient, project_id=project_id, task_id=task_id, timezone=UTC)
        reading: ReadingRun = ReadingRun(TaskReadTools(source), TaskReadMap(source, scenario=ReadScenario.RISK_ANALYSIS))
        today = datetime.now(UTC).date()
        plan: ReadPlan = ReadPlan((ReadStep(ReadTool.CARD), ReadStep(ReadTool.COMMENTS, today, today, 20)))
        result = reading.execute(plan)
        check("real authorized plan executes in order", result.stop is PlanStop.DONE
              and tuple(item.step.tool for item in result.items) == (ReadTool.CARD, ReadTool.COMMENTS))
        check("entire context includes unquoted message provenance", SourceRef("mail.message", message_id)
              in {ref for item in reading.items for ref in item.sources})
        verifier: OdooContextVerifier = OdooContextVerifier(recipient, project_id=project_id, task_id=task_id, timezone=UTC)
        gate: SourceGate = SourceGate(reading, verifier)
        candidate: Candidate = Candidate("Synthetic interpretation", (SourceRef("project.task", task_id),))
        check("fresh cursor accepts authorized complete context", gate.validate(candidate)[0] is GateStatus.AVAILABLE)
        check("unknown source never releases text", gate.validate(Candidate("Synthetic", (SourceRef("mail.message", message_id + 999999),)))
              == (GateStatus.INVALID, None))
        check("same step cannot be repeated in another plan", reading.execute(ReadPlan((plan.steps[0],))).stop is PlanStop.DUPLICATE)
        with env.registry.cursor() as cursor:
            administrator: Environment = Environment(cursor, 1, {}, su=True)
            rule: BaseModel = administrator["ir.access"].create({
                "name": token, "model_id": administrator["ir.model"]._get_id("mail.message"),
                "operation": "r", "domain": repr([("id", "!=", message_id)]),
            })
            rule_id = rule.id
            cursor.commit()
        check("committed revocation of an uncited source blocks all text", gate.validate(candidate) == (GateStatus.DENIED, None))
        env.cr.rollback()
        env.invalidate_all()
        check("ordinary ORM confirms message restriction", not recipient["mail.message"].search([("id", "=", message_id)]))
        env["ir.access"].browse(rule_id).unlink()
        rule_id = 0
        env.cr.commit()
        check("restored rights permit a new current check", gate.validate(candidate)[0] is GateStatus.AVAILABLE)
        env["project.task"].browse(task_id).write({"description": "Changed synthetic description"})
        env.cr.commit()
        # Description was not passed to the model: unrelated edits do not taint this context.
        check("unread field change does not invalidate used context", gate.validate(candidate)[0] is GateStatus.AVAILABLE)
        env["project.task"].browse(task_id).write({"name": token + " edited"})
        env.cr.commit()
        check("changed context value blocks stale generated text", gate.validate(candidate) == (GateStatus.DENIED, None))
    finally:
        env.cr.rollback()
        env.invalidate_all()
        if rule_id:
            env["ir.access"].browse(rule_id).exists().unlink()
        if project_id:
            env["project.project"].browse(project_id).exists().unlink()
        if user_id:
            env["res.users"].browse(user_id).exists().unlink()
        if partner_id:
            env["res.partner"].browse(partner_id).exists().unlink()
        env.cr.commit()
        check("no synthetic project remains", not env["project.project"].with_context(active_test=False).search_count([("name", "=", token)]))
        check("no synthetic user remains", not env["res.users"].with_context(active_test=False).search_count([("login", "=", token)]))
        check("no synthetic rule remains", not env["ir.access"].search_count([("name", "=", token)]))
        check("no synthetic partner remains", not env["res.partner"].browse(partner_id).exists())
        env.cr.rollback()
    print("PASS synthetic cross-transaction cleanup completed; checks", passed)
    return passed
