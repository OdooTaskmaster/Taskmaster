"""Define persistence contracts for background operations, independent of SQLite."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class OperationState(StrEnum):
    """Name a persisted operation state; OperationState('accepted') returns ACCEPTED."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OperationError(StrEnum):
    """Carry a safe failure category, never raw source text.

    Example: OperationError('source_unavailable') returns SOURCE_UNAVAILABLE.
    """

    SOURCE_UNAVAILABLE = "source_unavailable"
    EXECUTION_FAILED = "execution_failed"
    DISPATCH_FAILED = "dispatch_failed"


class StateStoreError(Exception):
    """Report an unavailable or invalid store without internal details.

    Example: raise StateStoreError() produces a fixed safe message.
    """

    def __init__(self) -> None:
        """Construct a fixed message with no inputs; StateStoreError() returns the error."""
        super().__init__("State storage is unavailable.")


class StateConflict(Exception):
    """Reject a conflicting key or stale transition without exposing stored data.

    Example: raise StateConflict() reports a conflict without task content.
    """

    def __init__(self) -> None:
        """Construct a fixed message with no inputs; StateConflict() returns the error."""
        super().__init__("The stored state conflicts with this request.")


@dataclass(frozen=True, slots=True)
class Operation:
    """Carry operation identity, scope, state, timestamps and a result reference.

    Timestamps are aware UTC. result_id exists only on success, error only on
    failure. Example: store.accept('op-1', owner_id=7, project_id=3,
    recipient_id=7) returns an accepted Operation with these immutable IDs.
    """

    operation_id: str
    owner_id: int
    project_id: int
    recipient_id: int
    state: OperationState
    created_at: datetime
    updated_at: datetime
    result_id: str | None = None
    error: OperationError | None = None
    company_ids: tuple[int, ...] | None = None
    project_timezone: str | None = None


def valid_company_ids(company_ids: object) -> bool:
    """Validate an ordered, nonempty tuple of distinct Odoo company IDs.

    Example: valid_company_ids((3, 1)) returns True.
    """
    return (type(company_ids) is tuple and bool(company_ids)
            and all(type(item) is int and 0 < item <= 2**31 - 1 for item in company_ids)
            and len(set(company_ids)) == len(company_ids))


class OperationStore(Protocol):
    """Persist operations via a replaceable boundary.

    Example: store: OperationStore = SQLiteStateStore(path).
    """

    def accept(self, operation_id: str, *, owner_id: int, project_id: int, recipient_id: int) -> Operation:
        """Return a new accepted operation or the existing identical scoped operation.

        Example: store.accept('op-1', owner_id=7, project_id=3, recipient_id=7).
        """
        ...

    def get_operation(self, operation_id: str, *, owner_id: int, project_id: int) -> Operation | None:
        """Return an operation only for this owner/project, otherwise None.

        Current Odoo permissions are the caller's responsibility (TECH-09).
        Example: store.get_operation('op-1', owner_id=7, project_id=3).
        """
        ...

    def transition(
        self, operation_id: str, *, expected: OperationState, target: OperationState,
        result_id: str | None = None, error: OperationError | None = None,
    ) -> Operation:
        """Atomically advance an operation or reject a stale/conflicting update.

        The trusted worker supplies an expected state and terminal metadata.
        Claiming RUNNING has exactly one winner. Only terminal repeats of an
        allowed transition are idempotent.
        Example: store.transition('op-1', expected=OperationState.ACCEPTED,
        target=OperationState.RUNNING) returns the running operation.
        """
        ...
