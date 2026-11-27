"""Exercise the installed morning cron with controlled instants on a disposable Odoo database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

if TYPE_CHECKING:
    from odoo.api import Environment


def run(env: Environment, *, expected_database: str) -> None:
    """Verify and roll back the fixtures; e.g. run(env, expected_database='taskmaster_morning_test')."""
    from odoo.exceptions import AccessError  # noqa: PLC0415
    from odoo.fields import Command  # noqa: PLC0415
    from odoo.modules import module  # noqa: PLC0415
    from odoo.tests.common import TransactionCase  # noqa: PLC0415
    from odoo.tools import config  # noqa: PLC0415

    from odoo.addons.taskmaster.adapters.odoo_background import OdooSummaryRunner  # noqa: PLC0415
    from odoo.addons.taskmaster.adapters.sqlite_state_store import SQLiteStateStore  # noqa: PLC0415
    from odoo.addons.taskmaster.application.background_operations import BackgroundOperations  # noqa: PLC0415
    from odoo.addons.taskmaster.application.operation_store import OperationState  # noqa: PLC0415
    from odoo.addons.taskmaster.models import project_morning  # noqa: PLC0415

    if (not expected_database.startswith("taskmaster_morning_") or env.cr.dbname != expected_database
            or environ.get("TASKMASTER_VERIFY_DB") != expected_database
            or environ.get("TASKMASTER_VERIFY_CONFIRM") != "DISPOSABLE_ROLLBACK_ONLY"
            or not env.su or config.get("dbfilter") or env["project.project"].with_context(active_test=False).search_count([])):
        raise ValueError("Use a clean administrator shell on a new dedicated Odoo test database.")

    check: TestCase = TestCase()
    due: datetime = datetime(2026, 11, 28, tzinfo=UTC)
    cron = env.ref("taskmaster.ir_cron_taskmaster_morning")
    check.assertTrue(cron.active)
    check.assertEqual((cron.interval_number, cron.interval_type), (1, "minutes"))

    try:
        groups: dict[str, list[int]] = {
            role: [env.ref("base.group_user").id, env.ref(f"project.group_project_{role}").id]
            for role in ("manager", "user")
        }
        manager, member = env["res.users"].with_context(no_reset_password=True, tracking_disable=True).create([{
            "name": f"Morning probe {role}", "login": f"morning-probe-{uuid4().hex}", "tz": "UTC",
            "company_id": env.company.id, "company_ids": [Command.set(env.company.ids)],
            "group_ids": [Command.set(group_ids)],
        } for role, group_ids in groups.items()])
        check.assertFalse(manager.with_user(manager).has_group("base.group_system"))
        settings: dict[str, object] = {
            "name": "Morning probe", "company_id": env.company.id, "taskmaster_morning_enabled": True,
            "taskmaster_morning_timezone": "Asia/Tokyo", "taskmaster_morning_recipient_id": manager.id,
        }
        other_company = env["res.company"].create({"name": "Morning probe other company"})
        project, missing_zone, missing_user, wrong_role, wrong_company, _, _, _ = env["project.project"].with_context(
            tracking_disable=True,
        ).create([
            settings, settings | {"taskmaster_morning_timezone": False},
            settings | {"taskmaster_morning_recipient_id": False},
            settings | {"taskmaster_morning_recipient_id": member.id},
            settings | {"company_id": other_company.id},
            settings | {"taskmaster_morning_enabled": False}, settings | {"active": False},
            settings | {"is_template": True},
        ])
        with check.assertRaises(AccessError), env.cr.savepoint():
            project.with_user(manager).write({"taskmaster_morning_enabled": False})
        duplicate = project.copy()
        check.assertFalse(duplicate.taskmaster_morning_enabled)
        check.assertFalse(duplicate.taskmaster_morning_timezone)
        check.assertFalse(duplicate.taskmaster_morning_recipient_id)
        env.flush_all()
        with (
            TemporaryDirectory() as folder,
            patch.object(module, "current_test", TransactionCase()),
            TransactionCase.registry_test_mode(cr=env.cr, registry=env.registry),
        ):
            path: Path = Path(folder) / "state.sqlite3"
            with patch.object(project_morning, "database_state_path", return_value=path), patch.object(
                project_morning, "_utc_now", return_value=due - timedelta(seconds=1),
            ) as clock, check.assertLogs(project_morning.__name__, level="WARNING") as logs:
                check.assertIs(cron.method_direct_trigger(), True)
                check.assertFalse(path.exists())
                clock.return_value = due
                check.assertIs(cron.method_direct_trigger(), True)
                store = SQLiteStateStore(path)
                operation_id, = store.due_dispatches(due, limit=10)
                operation = store.get_background_operation(operation_id)
                check.assertIsNotNone(operation)
                check.assertEqual((operation.owner_id, operation.project_id, operation.recipient_id),
                                  (manager.id, project.id, manager.id))
                check.assertEqual(operation.company_ids, tuple(env.company.ids))
                check.assertEqual(operation.project_timezone, "Asia/Tokyo")
                check.assertIs(cron.method_direct_trigger(), True)
                check.assertEqual(store.due_dispatches(due, limit=10), (operation_id,))
                check.assertEqual(store.get_background_operation(operation_id), operation)

                worker = BackgroundOperations(store, lambda key: key == operation_id, clock=lambda: due)
                check.assertEqual(worker.recover(), 1)
                worker.execute(operation_id, OdooSummaryRunner(store, env, expected_database, clock=lambda: due))
                succeeded = store.get_background_operation(operation_id)
                check.assertIsNotNone(succeeded)
                check.assertEqual(succeeded.state, OperationState.SUCCEEDED)
                check.assertIs(cron.method_direct_trigger(), True)
                check.assertEqual(store.due_dispatches(due, limit=10), ())
                check.assertEqual(store.get_background_operation(operation_id), succeeded)
            for record, reason in ((missing_zone, "missing_timezone"), (missing_user, "missing_recipient"),
                                   (wrong_role, "recipient_unavailable"), (wrong_company, "recipient_unavailable")):
                check.assertIn(f"project_id={record.id} reason={reason}", "\n".join(logs.output))
    finally:
        env.cr.rollback()
