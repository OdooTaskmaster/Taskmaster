"""Configure and run the Odoo timer for opted-in projects."""

from datetime import datetime
import logging
from pathlib import Path

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, MissingError

from ..adapters.odoo_background import _utc_now, database_state_path
from ..adapters.odoo_task_reader import readable_projects
from ..adapters.sqlite_state_store import SQLiteStateStore
from ..application.morning_schedule import ScheduleRefused, schedule_morning_check
from ..application.operation_store import Operation, StateConflict, StateStoreError

_LOGGER: logging.Logger = logging.getLogger(__name__)


class ProjectMorning(models.Model):
    """Extend project setup with the schedule; e.g. configure it in Project Settings."""

    _inherit = "project.project"

    taskmaster_morning_enabled = fields.Boolean(string="Morning risk check", groups="base.group_system", copy=False)
    taskmaster_morning_timezone = fields.Char(string="Project time zone (IANA)", groups="base.group_system", copy=False)
    taskmaster_morning_recipient_id = fields.Many2one(
        "res.users", string="Approved risk check recipient", groups="base.group_system", copy=False,
        domain=[("active", "=", True), ("share", "=", False)],
    )

    def _taskmaster_recipient_scope(self, recipient_id: int) -> tuple[int, ...] | None:
        """Return a company scope only for an active, authorized Project manager.

        Read access as the nominated user without elevation. For example,
        project._taskmaster_recipient_scope(7) returns (3,) or None on denial.
        """
        if recipient_id == SUPERUSER_ID:
            return None
        actor: api.Environment = api.Environment(self.env.cr, recipient_id, {}, su=False)
        try:
            if not (actor.user.active and actor.user.has_group("base.group_user")
                    and actor.user.has_group("project.group_project_manager")):
                return None
            company_id: int = self.company_id.id or actor.user.company_id.id
            scoped: api.Environment = actor(context={"allowed_company_ids": [company_id]})
            if scoped.company.id != company_id or not readable_projects(scoped, self.id):
                return None
        except (AccessError, MissingError):
            return None
        return (company_id,)

    @api.model
    def _cron_taskmaster_morning(self) -> None:
        """Evaluate enabled projects at one UTC instant and log safe refusal codes.

        Invoke as `model._cron_taskmaster_morning()` from the installed cron.
        Persist only queue eligibility; the existing worker dispatches and executes.
        """
        now: datetime = _utc_now()
        path: Path = database_state_path(self.env.cr.dbname)
        store: SQLiteStateStore | None = None

        def open_store() -> SQLiteStateStore:
            """Open SQLite lazily, reusing the store within the tick."""
            nonlocal store
            if store is None:
                path.parent.mkdir(mode=0o700, exist_ok=True)
                store = SQLiteStateStore(path)
            return store

        for project in self.with_context(active_test=True).search([
            ("taskmaster_morning_enabled", "=", True), ("is_template", "=", False),
        ]):
            try:
                operation: Operation | None = schedule_morning_check(
                    project.id, project.taskmaster_morning_timezone or None,
                    project.taskmaster_morning_recipient_id.id or None,
                    now=now, authorize=project._taskmaster_recipient_scope, open_store=open_store,
                )
                if operation is not None:
                    _LOGGER.debug("Morning risk check slot: project_id=%s operation_id=%s", project.id, operation.operation_id)
            except ScheduleRefused as error:
                _LOGGER.warning("Morning risk check refused: project_id=%s reason=%s", project.id, error)
            except StateConflict:
                _LOGGER.warning("Morning risk check refused: project_id=%s reason=slot_scope_conflict", project.id)
            except (OSError, StateStoreError):
                _LOGGER.error("Morning risk check refused: project_id=%s reason=state_unavailable", project.id)
