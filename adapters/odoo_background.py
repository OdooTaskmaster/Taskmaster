"""Execute one queued summary operation inside a trusted Odoo database context."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, tzinfo
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ._core import Operation, risk_summary as core, summary_store as summaries
from .odoo_summary_source import read_summary_tasks
from .odoo_task_reader import readable_projects

if TYPE_CHECKING:
    from odoo.api import Environment

_PILOT_TIMEZONE: str = "Europe/Moscow"
_INTERNAL_GROUP: str = "base.group_user"
_QUEUE_PREFIX: str = "taskmaster."


class SummaryExecutionFailed(Exception):
    """Report a failed summary as a generic execution error.

    Example: raise SummaryExecutionFailed() when a summary has FAILED status.
    """

    def __init__(self) -> None:
        """Construct a fixed safe message; SummaryExecutionFailed() returns the error."""
        super().__init__("Summary execution failed.")


def _execution_denied() -> Exception:
    """Build the shared denial error from the application module.

    Example: _execution_denied() returns an ExecutionDenied instance.
    """
    if __package__ and "." in __package__:
        from ..application.background_operations import ExecutionDenied  # noqa: PLC0415
    else:
        from application.background_operations import ExecutionDenied  # noqa: PLC0415
    return ExecutionDenied()


def queue_name(database: str) -> str:
    """Return the server-generated queue name for one trusted database.

    Example: queue_name('pilot') returns 'taskmaster.<sha256 hex>'.
    """
    if not isinstance(database, str) or not database.strip():
        raise ValueError("A database name is required.")
    return f"{_QUEUE_PREFIX}{sha256(database.encode('utf-8')).hexdigest()}"


def database_state_path(database: str) -> Path:
    """Return the shared per-database state file from the sync controller.

    Example: database_state_path('pilot') equals
    controllers.summary.state_path('pilot').
    """
    if not isinstance(database, str) or not database.strip():
        raise ValueError("A database name is required.")
    if __package__ and "." in __package__:
        from ..controllers.summary import state_path  # noqa: PLC0415
    else:
        from controllers.summary import state_path  # noqa: PLC0415
    return state_path(database)


@contextmanager
def _recipient_environment(env: "Environment", expected_db: str, user_id: int) -> Iterator["Environment"]:
    """Yield a fresh non-elevated environment for a stored active internal user.

    Bind the registry to the expected database and never to message payload.
    The fresh environment uses the stored actor's Odoo companies; project
    membership and record rules still enforce the selected project scope.
    Example: with _recipient_environment(env, 'pilot', 7) as recipient: ...
    """
    from odoo import SUPERUSER_ID  # noqa: PLC0415
    from odoo.api import Environment  # noqa: PLC0415

    if type(user_id) is not int or user_id <= 0 or user_id == SUPERUSER_ID:
        raise _execution_denied()
    if env.cr.dbname != expected_db:
        raise _execution_denied()
    with env.registry.cursor() as cursor:
        if cursor.dbname != expected_db:
            raise _execution_denied()
        recipient: Environment = Environment(cursor, user_id, {}, su=False)
        if recipient.cr.dbname != expected_db or recipient.su or recipient.uid != user_id:
            raise _execution_denied()
        if not recipient.user.active or not recipient.user.has_group(_INTERNAL_GROUP):
            raise _execution_denied()
        try:
            yield recipient
        finally:
            cursor.rollback()


class OdooSummaryRunner:
    """Run the pilot summary for one operation in a fresh Odoo context.

    Example: OdooSummaryRunner(store, env, 'pilot') returns a callable for
    BackgroundOperations.execute.
    """

    def __init__(
        self, store: summaries.SummaryStore, env: "Environment", expected_db: str, *,
        project_timezone: tzinfo | None = None, clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind the store, shell environment and trusted database name.

        Use the sync default Europe/Moscow timezone unless a ZoneInfo is passed.
        Example: OdooSummaryRunner(store, env, 'pilot') uses Europe/Moscow.
        """
        if not isinstance(expected_db, str) or not expected_db.strip():
            raise ValueError("An expected database name is required.")
        if env.cr.dbname != expected_db:
            raise ValueError("The shell must be bound to the expected database.")
        self._store: summaries.SummaryStore = store
        self._env: "Environment" = env
        self._expected_db: str = expected_db
        self._timezone: tzinfo = project_timezone if project_timezone is not None else ZoneInfo(_PILOT_TIMEZONE)
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now

    def __call__(self, operation: Operation) -> str:
        """Return the saved result ID for the operation or raise a safe error.

        Accept only owner_id == recipient_id for the pilot. Open a fresh
        non-elevated environment for the stored recipient, require current
        project access, build the summary, and save it under operation_id.
        Raise ExecutionDenied when the actor or rights are invalid and
        SummaryExecutionFailed when the summary cannot be built. Write no Odoo
        records. Example: runner(operation) returns 'result-id'.
        """
        if operation.owner_id != operation.recipient_id:
            raise _execution_denied()
        if (type(operation.recipient_id) is not int or operation.recipient_id <= 0
                or type(operation.project_id) is not int or operation.project_id <= 0
                or not isinstance(operation.operation_id, str) or not operation.operation_id.strip()):
            raise _execution_denied()
        with _recipient_environment(self._env, self._expected_db, operation.recipient_id) as recipient:
            if not readable_projects(recipient, operation.project_id):
                raise _execution_denied()
            checked_at: datetime = self._clock()
            summary: core.ProjectRiskSummary = core.build_project_risk_summary(
                lambda: read_summary_tasks(recipient, operation.project_id),
                checked_at=checked_at, project_timezone=self._timezone,
            )
            if summary.status is core.SummaryStatus.FAILED:
                raise SummaryExecutionFailed()
            saved: summaries.SavedSummary = self._store.save_summary(
                operation.operation_id, project_id=operation.project_id,
                recipient_id=operation.recipient_id, checked_at=checked_at, summary=summary,
            )
            return saved.result_id


def _utc_now() -> datetime:
    """Return the current aware UTC instant; _utc_now() gives a timestamp."""
    return datetime.now(UTC)
