"""Persist pilot state in one local SQLite file per Odoo database."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ._core import (
    Operation, OperationError, OperationState, StateConflict, StateStoreError,
    risk_summary as core, summary_store as summaries,
)
from ._summary_codec import decode_summary, encode_summary

if __package__ and "." in __package__:
    from ..application.operation_store import valid_company_ids
else:
    from application.operation_store import valid_company_ids


class SQLiteStateStore:
    """Open short-lived SQLite connections using DELETE/EXTRA and a 3s busy wait.

    Supply a local file path whose parent already exists with restricted access.
    The server must assign a different file to each Odoo database. Construction
    initializes tables, not Odoo data. Example: SQLiteStateStore(path) returns
    a store without keeping a live connection between calls.
    """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        """Open the local store without initializing it when read_only is True.

        Example: SQLiteStateStore(path, read_only=True) never creates the file.
        """
        self._path: Path = Path(path)
        self._read_only: bool = read_only
        if read_only:
            return
        with self._connection() as connection:
            mode: str = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            if mode != "delete":
                raise StateStoreError()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS operation (
                    operation_id TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('accepted', 'running', 'succeeded', 'failed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_id TEXT,
                    error TEXT,
                    dispatch_due_at REAL,
                    dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                    dispatch_confirmed INTEGER NOT NULL DEFAULT 0,
                    company_ids TEXT,
                    CHECK ((state = 'succeeded' AND result_id IS NOT NULL AND error IS NULL)
                        OR (state = 'failed' AND result_id IS NULL AND error IS NOT NULL)
                        OR (state IN ('accepted', 'running') AND result_id IS NULL AND error IS NULL))
                )
            """)
            columns: set[str] = {row["name"] for row in connection.execute("PRAGMA table_info(operation)")}
            if "dispatch_due_at" not in columns:
                connection.execute("ALTER TABLE operation ADD COLUMN dispatch_due_at REAL")
                connection.execute("ALTER TABLE operation ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0")
                connection.execute("ALTER TABLE operation ADD COLUMN dispatch_confirmed INTEGER NOT NULL DEFAULT 0")
            if "company_ids" not in columns:
                connection.execute("ALTER TABLE operation ADD COLUMN company_ids TEXT")
            connection.execute("""
                CREATE INDEX IF NOT EXISTS operation_dispatch_due ON operation (dispatch_due_at, operation_id)
                WHERE state = 'accepted' AND dispatch_confirmed = 0 AND dispatch_due_at IS NOT NULL
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS summary (
                    result_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    project_id INTEGER NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE (run_id, project_id, recipient_id)
                )
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection and always close it, sanitizing failures.

        Example: with store._connection() as connection: ... commits on success.
        """
        try:
            location: Path | str = self._path.absolute().as_uri() + "?mode=ro" if self._read_only else self._path
            with closing(sqlite3.connect(location, uri=self._read_only, timeout=3, isolation_level="IMMEDIATE")) as connection:
                connection.row_factory = sqlite3.Row
                if self._read_only:
                    if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                        raise StateStoreError()
                else:
                    connection.execute("PRAGMA synchronous=EXTRA")
                with connection:
                    yield connection
        except (sqlite3.Error, OSError):
            raise StateStoreError() from None

    @staticmethod
    def _operation(row: sqlite3.Row) -> Operation:
        """Decode a stored operation or fail safely on corrupt metadata.

        Example: store._operation(row) returns an immutable Operation.
        """
        try:
            company_ids: tuple[int, ...] | None = None
            if "company_ids" in row.keys() and row["company_ids"] is not None:
                decoded: object = json.loads(row["company_ids"])
                if not isinstance(decoded, list) or not valid_company_ids(tuple(decoded)):
                    raise ValueError
                company_ids = tuple(decoded)
            operation: Operation = Operation(
                row["operation_id"], row["owner_id"], row["project_id"], row["recipient_id"],
                OperationState(row["state"]), datetime.fromisoformat(row["created_at"]),
                datetime.fromisoformat(row["updated_at"]), row["result_id"],
                OperationError(row["error"]) if row["error"] is not None else None,
                company_ids,
            )
            if (operation.created_at.utcoffset() is None or operation.updated_at.utcoffset() is None
                    or operation.updated_at < operation.created_at
                    or operation.state is OperationState.SUCCEEDED and (
                        not isinstance(operation.result_id, str) or not operation.result_id.strip() or operation.error is not None
                    )
                    or operation.state is OperationState.FAILED and (operation.result_id is not None or operation.error is None)
                    or operation.state in (OperationState.ACCEPTED, OperationState.RUNNING) and (
                        operation.result_id is not None or operation.error is not None
                    )):
                raise ValueError
            return operation
        except (ValueError, TypeError):
            raise StateStoreError() from None

    def accept(self, operation_id: str, *, owner_id: int, project_id: int, recipient_id: int) -> Operation:
        """Persist an accepted operation; repeat keys require identical scope.

        Example: store.accept('op-1', owner_id=7, project_id=3, recipient_id=7)
        returns a new or previously accepted operation, never resets it.
        """
        return self._accept(operation_id, owner_id, project_id, recipient_id, dispatch_due_at=None)

    def accept_background(self, operation_id: str, *, owner_id: int, project_id: int,
                          recipient_id: int, now: datetime, company_ids: tuple[int, ...] | None = None) -> Operation:
        """Persist the operation and its dispatch eligibility in one SQLite transaction.

        Pass server-established scope, optional ordered companies and an aware clock. Example:
        store.accept_background('op-1', owner_id=7, project_id=3, recipient_id=7,
        now=now) returns the accepted operation and makes it eligible for sending.
        """
        if now.utcoffset() is None:
            raise ValueError("The dispatch clock must be timezone-aware.")
        return self._accept(operation_id, owner_id, project_id, recipient_id, dispatch_due_at=now.timestamp(),
                            company_ids=company_ids)

    def _accept(self, operation_id: str, owner_id: int, project_id: int,
                recipient_id: int, *, dispatch_due_at: float | None, company_ids: tuple[int, ...] | None = None) -> Operation:
        """Insert an operation for the scope and mode, or return an identical existing one.

        Example: store._accept('op-1', 7, 3, 7, dispatch_due_at=None) returns a plain operation.
        """
        if (not isinstance(operation_id, str) or not operation_id.strip()
                or any(type(value) is not int or value <= 0 for value in (owner_id, project_id, recipient_id))):
            raise ValueError("An operation key and positive scope IDs are required.")
        if company_ids is not None and not valid_company_ids(company_ids):
            raise ValueError("The ordered company scope is invalid.")
        now: str = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO operation (operation_id, owner_id, project_id, recipient_id, state,
                   created_at, updated_at, dispatch_due_at, company_ids) VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (operation_id, owner_id, project_id, recipient_id, now, now, dispatch_due_at,
                 json.dumps(company_ids) if company_ids is not None else None),
            )
            row: sqlite3.Row = connection.execute("SELECT * FROM operation WHERE operation_id = ?", (operation_id,)).fetchone()
            operation: Operation = self._operation(row)
            if ((operation.owner_id, operation.project_id, operation.recipient_id) != (owner_id, project_id, recipient_id)
                    or (row["dispatch_due_at"] is None) != (dispatch_due_at is None)
                    or operation.company_ids != company_ids):
                raise StateConflict()
            return operation

    def get_operation(self, operation_id: str, *, owner_id: int, project_id: int) -> Operation | None:
        """Return a persisted operation only in the supplied owner/project scope.

        Example: store.get_operation('op-1', owner_id=7, project_id=3) returns
        None for a missing operation or a different owner or project.
        """
        with self._connection() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM operation WHERE operation_id = ? AND owner_id = ? AND project_id = ?",
                (operation_id, owner_id, project_id),
            ).fetchone()
            return None if row is None else self._operation(row)

    def get_background_operation(self, operation_id: str) -> Operation | None:
        """Resolve the scope from this database for a trusted worker, not an HTTP caller.

        Example: store.get_background_operation('op-1') returns None for a legacy plain operation.
        """
        with self._connection() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM operation WHERE operation_id = ? AND dispatch_due_at IS NOT NULL", (operation_id,),
            ).fetchone()
            return None if row is None else self._operation(row)

    def reconcile_saved_summary(self, operation_id: str) -> None:
        """Link a running background operation to its committed summary, without reading its payload.

        Match the stored project and recipient, and leave other states unchanged.
        Example: store.reconcile_saved_summary('op-1') returns None even when no summary was saved.
        """
        with self._connection() as connection:
            connection.execute(
                """UPDATE operation AS job
                   SET state = 'succeeded', result_id = saved.result_id, updated_at = MAX(job.updated_at, ?)
                   FROM summary AS saved
                   WHERE job.operation_id = ? AND job.state = 'running' AND job.dispatch_due_at IS NOT NULL
                     AND saved.run_id = job.operation_id AND saved.project_id = job.project_id
                     AND saved.recipient_id = job.recipient_id""",
                (datetime.now(UTC).isoformat(), operation_id),
            )

    def due_dispatches(self, now: datetime, *, limit: int) -> tuple[str, ...]:
        """List up to limit accepted IDs lacking a confirmation and due for recovery.

        Example: store.due_dispatches(now, limit=10) returns ('op-1',) for one due job.
        """
        if now.utcoffset() is None or limit <= 0:
            raise ValueError("An aware clock and positive batch size are required.")
        with self._connection() as connection:
            return tuple(row["operation_id"] for row in connection.execute(
                """SELECT operation_id FROM operation WHERE state = 'accepted' AND dispatch_confirmed = 0
                   AND dispatch_due_at <= ? ORDER BY dispatch_due_at, operation_id LIMIT ?""",
                (now.timestamp(), limit),
            ))

    def reserve_dispatch(self, operation_id: str, *, now: datetime, retry_at: datetime, max_attempts: int) -> bool:
        """Reserve one attempt until retry_at, or fail an accepted job with no budget left.

        Pass aware times and a positive budget. Example: reserve_dispatch(...)
        returns True once for a due job and False for competing attempts.
        """
        if now.utcoffset() is None or retry_at.utcoffset() is None or retry_at <= now or max_attempts <= 0:
            raise ValueError("An aware retry window and positive attempt budget are required.")
        with self._connection() as connection:
            attempt: sqlite3.Cursor = connection.execute(
                """UPDATE operation SET dispatch_attempts = dispatch_attempts + 1, dispatch_due_at = ?
                   WHERE operation_id = ? AND state = 'accepted' AND dispatch_confirmed = 0
                     AND dispatch_due_at <= ? AND dispatch_attempts < ?""",
                (retry_at.timestamp(), operation_id, now.timestamp(), max_attempts),
            )
            if attempt.rowcount == 1:
                return True
            connection.execute(
                """UPDATE operation SET state = 'failed', error = ?, updated_at = MAX(updated_at, ?)
                   WHERE operation_id = ? AND state = 'accepted' AND dispatch_confirmed = 0
                     AND dispatch_due_at <= ? AND dispatch_attempts >= ?""",
                (OperationError.DISPATCH_FAILED.value, now.astimezone(UTC).isoformat(), operation_id,
                 now.timestamp(), max_attempts),
            )
            return False

    def confirm_dispatch(self, operation_id: str) -> None:
        """Record a broker confirmation without updating execution state or retry age.

        Example: store.confirm_dispatch('op-1') returns None and stops recovery for that ID.
        """
        with self._connection() as connection:
            connection.execute(
                "UPDATE operation SET dispatch_confirmed = 1 WHERE operation_id = ? AND dispatch_due_at IS NOT NULL",
                (operation_id,),
            )

    def transition(
        self, operation_id: str, *, expected: OperationState, target: OperationState,
        result_id: str | None = None, error: OperationError | None = None,
    ) -> Operation:
        """Advance with a serialized expected-state check, rejecting stale writes.

        Supply a result ID for success or an enum category for failure.
        Exact terminal repeats preserve the result and timestamps; a second
        attempt to claim RUNNING conflicts, so only one worker can start.
        Example: store.transition('op-1', expected=OperationState.RUNNING,
        target=OperationState.SUCCEEDED, result_id='summary-1') returns success.
        """
        if not isinstance(expected, OperationState) or not isinstance(target, OperationState):
            raise ValueError("Operation states must be explicit enum members.")
        if (target is OperationState.SUCCEEDED and (not isinstance(result_id, str) or not result_id.strip() or error is not None)
                or target is OperationState.FAILED and (result_id is not None or not isinstance(error, OperationError))
                or target in (OperationState.ACCEPTED, OperationState.RUNNING) and (result_id is not None or error is not None)):
            raise ValueError("Terminal metadata does not match the target state.")
        allowed: bool = (
            expected is OperationState.ACCEPTED and target in (OperationState.RUNNING, OperationState.FAILED)
            or expected is OperationState.RUNNING and target in (OperationState.SUCCEEDED, OperationState.FAILED)
        )
        if not allowed:
            raise StateConflict()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM operation WHERE operation_id = ?", (operation_id,),
            ).fetchone()
            if row is None:
                raise StateConflict()
            current: Operation = self._operation(row)
            if target in (OperationState.SUCCEEDED, OperationState.FAILED) and (
                current.state, current.result_id, current.error
            ) == (target, result_id, error):
                return current
            if current.state is not expected:
                raise StateConflict()
            connection.execute(
                "UPDATE operation SET state = ?, updated_at = ?, result_id = ?, error = ? WHERE operation_id = ?",
                (target.value, max(datetime.now(UTC), current.updated_at).isoformat(), result_id, error.value if error else None, operation_id),
            )
            return self._operation(connection.execute("SELECT * FROM operation WHERE operation_id = ?", (operation_id,)).fetchone())

    @staticmethod
    def _saved_summary(row: sqlite3.Row) -> summaries.SavedSummary:
        """Decode a stored result and its calculation time, failing safely on corruption.

        Example: store._saved_summary(row) returns the internal SavedSummary.
        """
        try:
            checked_at: datetime = datetime.fromisoformat(row["checked_at"])
            if checked_at.utcoffset() is None:
                raise ValueError
            return summaries.SavedSummary(
                row["result_id"], row["run_id"], row["project_id"], row["recipient_id"],
                checked_at, decode_summary(row["payload"]),
            )
        except (ValueError, TypeError):
            raise StateStoreError() from None

    def save_summary(
        self, run_id: str, *, project_id: int, recipient_id: int,
        checked_at: datetime, summary: core.ProjectRiskSummary,
    ) -> summaries.SavedSummary:
        """Persist an immutable summary or return the identical run's saved result.

        Supply positive scope IDs and the aware calculation time. The trusted
        caller has already enforced access when building this result.
        Example: store.save_summary('run-1', project_id=3, recipient_id=7,
        checked_at=now, summary=result) returns a stable result ID and payload.
        """
        if (not isinstance(run_id, str) or not run_id.strip()
                or any(type(value) is not int or value <= 0 for value in (project_id, recipient_id))
                or checked_at.utcoffset() is None):
            raise ValueError("A run key, positive scope IDs and an aware calculation time are required.")
        payload: str = encode_summary(summary)
        if decode_summary(payload) != summary:
            raise StateStoreError()
        checked_time: str = checked_at.astimezone(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO summary VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (str(uuid4()), run_id, project_id, recipient_id, checked_time, payload),
            )
            row: sqlite3.Row = connection.execute(
                "SELECT * FROM summary WHERE run_id = ? AND project_id = ? AND recipient_id = ?",
                (run_id, project_id, recipient_id),
            ).fetchone()
            saved: summaries.SavedSummary = self._saved_summary(row)
            if (saved.checked_at, saved.summary) != (checked_at, summary):
                raise StateConflict()
            return saved

    def get_summary(
        self, result_id: str, *, project_id: int, recipient_id: int, authorize: summaries.SummaryAuthorizer,
    ) -> summaries.SavedSummary | None:
        """Return the result only if its whole contents remain currently authorized.

        Use a trusted server checker for project, task, field and source rights;
        False or SummaryReadError denies everything, including aggregate state;
        an unexpected checker error raises a sanitized StateStoreError.
        The check runs outside the storage transaction and on every call.
        Example: store.get_summary('id', project_id=3, recipient_id=7,
        authorize=server_access_check) returns None for a revoked task.
        """
        return self._read_summary(
            "SELECT * FROM summary WHERE result_id = ? AND project_id = ? AND recipient_id = ?",
            (result_id, project_id, recipient_id), authorize,
        )

    def get_latest_summary(
        self, *, project_id: int, recipient_id: int, authorize: summaries.SummaryAuthorizer,
    ) -> summaries.SavedSummary | None:
        """Authorize the newest saved result in scope without falling back on denial.

        Example: store.get_latest_summary(project_id=3, recipient_id=7,
        authorize=check) returns None when the latest result is unavailable.
        """
        return self._read_summary(
            "SELECT * FROM summary WHERE project_id = ? AND recipient_id = ? "
            "ORDER BY checked_at DESC, result_id DESC LIMIT 1",
            (project_id, recipient_id), authorize,
        )

    def _read_summary(
        self, sql: str, values: tuple[str | int, ...], authorize: summaries.SummaryAuthorizer,
    ) -> summaries.SavedSummary | None:
        """Run a fixed internal query and authorize its result outside SQLite.

        Example: get_latest_summary uses this helper to check just one result.
        """
        with self._connection() as connection:
            row: sqlite3.Row | None = connection.execute(sql, values).fetchone()
            if row is None:
                return None
            saved: summaries.SavedSummary = self._saved_summary(row)
        try:
            return saved if authorize(saved) is True else None
        except core.SummaryReadError:
            return None
        except Exception:
            raise StateStoreError() from None
