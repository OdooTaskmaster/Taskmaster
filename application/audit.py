"""Record audit events that carry no project text or secrets."""

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import ErrorCategory
from .model_port import ModelFailure

LOGGER_NAME: str = "odoo.addons.taskmaster"
AUDIT_PREFIX: str = "taskmaster.audit "
FIELD_LIMIT: int = 120


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

    The record has no field for task text, prompts or secrets, so a caller
    cannot put them into the journal by accident.
    Example: AuditEvent(AuditAction.AGENT_RUN, AuditOutcome.SUCCEEDED, "op-1")
    """

    action: AuditAction
    outcome: AuditOutcome
    operation_id: str
    actor_id: int | None = None
    model: str | None = None
    duration_ms: int | None = None
    error_category: ErrorCategory | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            message: str = "operation_id must not be empty"
            raise ValueError(message)

    def as_fields(self) -> dict[str, Any]:
        """Return only the allow-listed fields for the journal.

        Example: AuditEvent(...).as_fields()["operation_id"] == "op-1"
        """

        fields: dict[str, Any] = {
            "action": self.action.value,
            "outcome": self.outcome.value,
            "operation_id": _clean(self.operation_id),
        }
        if self.actor_id is not None:
            fields["actor_id"] = self.actor_id
        if self.model is not None:
            fields["model"] = _clean(self.model)
        if self.duration_ms is not None:
            fields["duration_ms"] = self.duration_ms
        if self.error_category is not None:
            fields["error_category"] = self.error_category.value
        if self.status_code is not None:
            fields["status_code"] = self.status_code
        return fields


def failure_event(
    action: AuditAction,
    operation_id: str,
    error: BaseException,
    *,
    actor_id: int | None = None,
    model: str | None = None,
    duration_ms: int | None = None,
) -> AuditEvent:
    """Build a failed event without copying the exception message.

    Example: failure_event(AuditAction.MODEL_CALL, "op-1", error) reports
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
        model=model,
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


def _clean(value: str) -> str:
    """Collapse whitespace and cap the length of one text field."""

    return " ".join(value.split())[:FIELD_LIMIT]
