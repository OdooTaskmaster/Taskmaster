"""Record audit events that carry no project text or secrets."""

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from .errors import ErrorCategory
from .model_port import ModelFailure

LOGGER_NAME: str = "odoo.addons.taskmaster"
AUDIT_PREFIX: str = "taskmaster.audit "


class AuditAction(StrEnum):
    """Name an auditable action.

    Example: AuditAction.AGENT_RUN marks one agent invocation.
    """

    USER_ACTION = "user_action"
    AGENT_RUN = "agent_run"
    MODEL_CALL = "model_call"


class AuditOutcome(StrEnum):
    """Name the result of an auditable action.

    Example: AuditOutcome.FAILED marks a rejected or broken action.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Describe one audit record with allow-listed fields only.

    Use an independently generated UUID for correlation; do not derive it from
    project data. Model names and other free text are deliberately excluded.
    Example: AuditEvent(AuditAction.AGENT_RUN, AuditOutcome.SUCCEEDED, uuid4())
    """

    action: AuditAction
    outcome: AuditOutcome
    operation_id: UUID
    actor_id: int | None = None
    duration_ms: int | None = None
    error_category: ErrorCategory | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, UUID) or self.operation_id.version != 4:
            message: str = "operation_id must be a random UUID v4"
            raise ValueError(message)
        if not isinstance(self.action, AuditAction) or not isinstance(self.outcome, AuditOutcome):
            message = "invalid audit action or outcome"
            raise TypeError(message)
        if self.error_category is not None and not isinstance(self.error_category, ErrorCategory):
            message = "invalid error category"
            raise TypeError(message)
        for value in (self.actor_id, self.duration_ms, self.status_code):
            if value is not None and (type(value) is not int or value < 0):
                message = "audit numeric fields must be non-negative integers"
                raise ValueError(message)

    def as_fields(self) -> dict[str, Any]:
        """Return only the allow-listed fields for the journal.

        Example: AuditEvent(...).as_fields()["operation_id"] == "op-1"
        """

        fields: dict[str, Any] = {
            "action": self.action.value,
            "outcome": self.outcome.value,
            "operation_id": str(self.operation_id),
        }
        if self.actor_id is not None:
            fields["actor_id"] = self.actor_id
        if self.duration_ms is not None:
            fields["duration_ms"] = self.duration_ms
        if self.error_category is not None:
            fields["error_category"] = self.error_category.value
        if self.status_code is not None:
            fields["status_code"] = self.status_code
        return fields


def failure_event(
    action: AuditAction,
    operation_id: UUID,
    error: BaseException,
    *,
    actor_id: int | None = None,
    duration_ms: int | None = None,
) -> AuditEvent:
    """Build a failed event without copying the exception message.

    Example: failure_event(AuditAction.MODEL_CALL, uuid4(), error) reports
    "unexpected" for a plain exception and the model category for ModelFailure.
    """

    if isinstance(error, ModelFailure):
        category: ErrorCategory = error.category
        status_code: int | None = error.status_code
    else:
        category = ErrorCategory.UNEXPECTED
        status_code = None
    return AuditEvent(
        action=action,
        outcome=AuditOutcome.FAILED,
        operation_id=operation_id,
        actor_id=actor_id,
        duration_ms=duration_ms,
        error_category=category,
        status_code=status_code,
    )


def log_event(event: AuditEvent, *, logger: logging.Logger | None = None) -> None:
    """Write one audit event as a single JSON line.

    Example: log_event(AuditEvent(...)) writes one INFO record to the add-on
    logger and returns None.
    """

    target: logging.Logger = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    payload: str = json.dumps(event.as_fields(), ensure_ascii=False, sort_keys=True)
    target.info("%s%s", AUDIT_PREFIX, payload)
