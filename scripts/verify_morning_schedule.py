"""Exercise the installed morning cron with controlled instants on a disposable Odoo database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
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

    if (not expected_database.startswith("taskmaster_morning_") or env.cr.dbname != expected_database
            or environ.get("TASKMASTER_VERIFY_DB") != expected_database
            or environ.get("TASKMASTER_VERIFY_CONFIRM") != "DISPOSABLE_ROLLBACK_ONLY"
            or not env.su or config.get("dbfilter") or env["project.project"].with_context(active_test=False).search_count([])):
        raise ValueError("Use a clean administrator shell on a new dedicated Odoo test database.")

    addon: str = Path(__file__).resolve().parents[1].name
    package: str = f"odoo.addons.{addon}"
    runner_type: type = import_module(f"{package}.adapters.odoo_background").OdooSummaryRunner
    store_type: type = import_module(f"{package}.adapters.sqlite_state_store").SQLiteStateStore
    worker_type: type = import_module(f"{package}.application.background_operations").BackgroundOperations
    operation_state: type = import_module(f"{package}.application.operation_store").OperationState
    project_morning = import_module(f"{package}.models.project_morning")
    check: TestCase = TestCase()
    due: datetime = datetime(2026, 11, 28, tzinfo=UTC)
    cron = env.ref(f"{addon}.ir_cron_taskmaster_morning")
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
        project.write({"allowed_internal_user_ids": [Command.link(member.id)]})
        check.assertEqual(project.with_user(member).taskmaster_morning_timezone, "Asia/Tokyo")
        with check.assertRaises(AccessError), env.cr.savepoint():
            project.with_user(manager).write({"taskmaster_morning_timezone": "UTC"})
        with check.assertRaises(AccessError), env.cr.savepoint():
            project.with_user(manager).create({"name": "Denied calendar", "taskmaster_morning_timezone": "UTC"})
        ordinary_project = project.with_user(manager).with_context(default_taskmaster_morning_timezone="UTC").create({
            "name": "Ordinary project with context default",
        })
        check.assertFalse(ordinary_project.taskmaster_morning_timezone)
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
                store = store_type(path)
                operation_id, = store.due_dispatches(due, limit=10)
                operation = store.get_background_operation(operation_id)
                check.assertIsNotNone(operation)
                check.assertEqual((operation.owner_id, operation.project_id, operation.recipient_id),
                                  (manager.id, project.id, manager.id))
                check.assertEqual(operation.company_ids, tuple(env.company.ids))
                check.assertEqual(operation.project_timezone, "Asia/Tokyo")
                check.assertTrue(operation.scheduled)
                check.assertIs(cron.method_direct_trigger(), True)
                check.assertEqual(store.due_dispatches(due, limit=10), (operation_id,))
                check.assertEqual(store.get_background_operation(operation_id), operation)

                worker = worker_type(store, lambda key: key == operation_id, clock=lambda: due)
                check.assertEqual(worker.recover(), 1)
                runner = runner_type(store, env, expected_database, clock=lambda: due)
                worker.execute(operation_id, runner)
                succeeded = store.get_background_operation(operation_id)
                check.assertIsNotNone(succeeded)
                check.assertEqual(succeeded.state, operation_state.SUCCEEDED)
                scheduled = store.get_summary(succeeded.result_id, project_id=project.id, recipient_id=manager.id,
                                              authorize=lambda _: True)
                check.assertEqual(scheduled.project_timezone, "Asia/Tokyo")
                check.assertIs(cron.method_direct_trigger(), True)
                check.assertEqual(store.due_dispatches(due, limit=10), ())
                check.assertEqual(store.get_background_operation(operation_id), succeeded)
                manual = store.accept_background("manual-timezone-probe", owner_id=member.id, project_id=project.id,
                                                 recipient_id=member.id, now=due, company_ids=tuple(env.company.ids),
                                                 project_timezone="Asia/Tokyo")
                check.assertFalse(manual.scheduled)
                manual_worker = worker_type(store, lambda key: key == manual.operation_id, clock=lambda: due)
                check.assertEqual(manual_worker.recover(), 1)
                manual_worker.execute(manual.operation_id, runner)
                manually_saved = store.get_background_operation(manual.operation_id)
                check.assertEqual(manually_saved.state, operation_state.SUCCEEDED)
                manual_summary = store.get_summary(manually_saved.result_id, project_id=project.id, recipient_id=member.id,
                                                   authorize=lambda _: True)
                check.assertEqual(manual_summary.project_timezone, scheduled.project_timezone)
            for record, reason in ((missing_zone, "missing_timezone"), (missing_user, "missing_recipient"),
                                   (wrong_role, "recipient_unavailable"), (wrong_company, "recipient_unavailable")):
                check.assertIn(f"project_id={record.id} reason={reason}", "\n".join(logs.output))
    finally:
        env.cr.rollback()
