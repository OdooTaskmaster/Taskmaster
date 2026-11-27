"""Dispatch and execute stored operations independently of AMQP and Odoo."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
import re
from typing import Protocol

from .operation_store import Operation, OperationError, OperationState, OperationStore, StateConflict, StateStoreError

_OPERATION_KEY: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_LOGGER: logging.Logger = logging.getLogger(__name__)


class ExecutionDenied(Exception):
    """Signal unavailable authorization without details; raise ExecutionDenied() to refuse work."""


class BackgroundStore(OperationStore, Protocol):
    """Persist dispatch metadata with an operation; SQLiteStateStore satisfies this port."""

    def accept_background(self, operation_id: str, *, owner_id: int, project_id: int,
                          recipient_id: int, now: datetime, company_ids: tuple[int, ...] | None = None) -> Operation:
        """Accept a scoped ID and mark it due at now; a same-key repeat returns the original."""
        ...

    def get_background_operation(self, operation_id: str) -> Operation | None:
        """Resolve a trusted background ID; return None for plain or missing operations."""
        ...

    def reconcile_saved_summary(self, operation_id: str) -> None:
        """Finish a running job with its saved summary; e.g. reconcile_saved_summary('op-1') returns None."""
        ...

    def due_dispatches(self, now: datetime, *, limit: int) -> tuple[str, ...]:
        """List at most limit due IDs; return () when none await a confirmation."""
        ...

    def reserve_dispatch(self, operation_id: str, *, now: datetime, retry_at: datetime, max_attempts: int) -> bool:
        """Reserve a due attempt until retry_at; return False if ineligible or exhausted."""
        ...

    def confirm_dispatch(self, operation_id: str) -> None:
        """Record confirmed publication for this ID; return None without changing execution state."""
        ...


class BackgroundOperations:
    """Track publications and let only a successful SQLite claim invoke the scenario."""

    def __init__(self, store: BackgroundStore, publisher: Callable[[str], bool], *,
                 retry_delay: timedelta = timedelta(seconds=30), max_attempts: int = 3,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        """Bind the database store and a publisher that returns True only on confirmation.

        Use a retry delay larger than the expected publish deadline, with an aware clock.
        Example: BackgroundOperations(store, queue.publish) uses three attempts by default.
        """
        if retry_delay <= timedelta() or max_attempts < 1:
            raise ValueError("A positive retry delay and attempt limit are required.")
        self._store: BackgroundStore = store
        self._publisher: Callable[[str], bool] = publisher
        self._delay: timedelta = retry_delay
        self._attempts: int = max_attempts
        self._clock: Callable[[], datetime] = clock

    def submit(self, operation_id: str, *, owner_id: int, project_id: int, recipient_id: int,
               company_ids: tuple[int, ...] | None = None) -> Operation:
        """Persist the server-established scope before attempting to enqueue the opaque ID.

        Persist the optional ordered company scope. Return the stored execution state after the dispatch attempt, not proof of
        delivery. Example: submit('op-1', owner_id=7, project_id=3, recipient_id=7)
        returns a failed operation if the attempt budget was exhausted.
        """
        if not isinstance(operation_id, str) or _OPERATION_KEY.fullmatch(operation_id) is None:
            raise ValueError("A valid operation key is required.")
        operation: Operation = self._store.accept_background(
            operation_id, owner_id=owner_id, project_id=project_id, recipient_id=recipient_id,
            now=self._clock(), company_ids=company_ids,
        )
        if operation.state is OperationState.ACCEPTED:
            self._dispatch(operation_id)
            current: Operation | None = self._store.get_background_operation(operation_id)
            if current is None:
                raise StateStoreError()
            return current
        return operation

    def recover(self, *, limit: int = 10) -> int:
        """Process a bounded batch of due operations; return the count of confirmations.

        Example: recover(limit=5) returns 0 when no publication is confirmed.
        """
        return sum(self._dispatch(key) for key in self._store.due_dispatches(self._clock(), limit=limit))

    def _dispatch(self, operation_id: str) -> bool:
        """Reserve and publish one ID; return True only after recording the confirmation.

        Example: _dispatch('op-1') returns False when the broker has not confirmed it.
        """
        now: datetime = self._clock()
        if not self._store.reserve_dispatch(
            operation_id, now=now, retry_at=now + self._delay, max_attempts=self._attempts,
        ):
            return False
        if self._publisher(operation_id) is not True:
            _LOGGER.warning("Background operation publication was not confirmed.")
            return False
        self._store.confirm_dispatch(operation_id)
        return True

    def execute(self, operation_id: str, run: Callable[[Operation], str]) -> None:
        """Run a claimed operation by trusted stored scope, with a saved result ID on success.

        The runner must enforce current rights and save the result before returning its ID.
        A duplicate can finalize a saved summary but never invokes the runner.
        Example: execute('op-1', run) returns None after handling the delivery.
        Storage failures while claiming or finishing propagate to the consumer.
        """
        if not isinstance(operation_id, str) or _OPERATION_KEY.fullmatch(operation_id) is None:
            return
        operation: Operation | None = self._store.get_background_operation(operation_id)
        if operation is None:
            return
        if operation.state is OperationState.RUNNING:
            self._store.reconcile_saved_summary(operation_id)
            return
        if operation.state is not OperationState.ACCEPTED:
            return
        try:
            claimed: Operation = self._store.transition(
                operation_id, expected=OperationState.ACCEPTED, target=OperationState.RUNNING,
            )
        except StateConflict:
            self._store.reconcile_saved_summary(operation_id)
            return
        error: OperationError
        try:
            result_id: str = run(claimed)
            if not isinstance(result_id, str) or not result_id.strip():
                raise ValueError("The scenario did not return a saved result ID.")
        except ExecutionDenied:
            error = OperationError.SOURCE_UNAVAILABLE
        except Exception:
            error = OperationError.EXECUTION_FAILED
        else:
            self._store.transition(
                operation_id, expected=OperationState.RUNNING, target=OperationState.SUCCEEDED, result_id=result_id,
            )
            return
        self._store.transition(operation_id, expected=OperationState.RUNNING, target=OperationState.FAILED, error=error)
        _LOGGER.warning("Background operation failed: %s.", error.value)
