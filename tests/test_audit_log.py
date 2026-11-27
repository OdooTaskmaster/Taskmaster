"""Verify audit events carry bounded metadata only."""

import json
import logging
from uuid import UUID, uuid4

import pytest
from application.audit import (
    AUDIT_PREFIX,
    LOGGER_NAME,
    AuditAction,
    AuditEvent,
    AuditOutcome,
    failure_event,
    log_event,
)
from application.errors import ErrorCategory
from application.model_port import ModelFailure

SECRET_VALUE: str = "sk-synthetic-0123456789abcdef"
TASK_TEXT: str = "Полный текст задачи клиента, который нельзя журналировать"


def test_events_correlate_without_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    operation_id: UUID = uuid4()
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    log_event(
        AuditEvent(
            AuditAction.USER_ACTION, AuditOutcome.SUCCEEDED, operation_id, actor_id=7,
        ),
    )
    log_event(AuditEvent(AuditAction.AGENT_RUN, AuditOutcome.SUCCEEDED, operation_id))
    log_event(
        failure_event(
            AuditAction.MODEL_CALL,
            operation_id,
            RuntimeError(f"{SECRET_VALUE} {TASK_TEXT}"),
        ),
    )
    payloads = [
        json.loads(record.getMessage().removeprefix(AUDIT_PREFIX))
        for record in caplog.records
    ]
    assert [p["action"] for p in payloads] == ["user_action", "agent_run", "model_call"]
    assert {p["operation_id"] for p in payloads} == {str(operation_id)}
    assert payloads[2]["error_category"] == "unexpected"
    assert SECRET_VALUE not in caplog.text and TASK_TEXT not in caplog.text
    assert "RuntimeError" not in caplog.text


def test_fields_and_model_failure_are_allow_listed() -> None:
    event = failure_event(
        AuditAction.MODEL_CALL,
        uuid4(),
        ModelFailure(ErrorCategory.PROVIDER_ERROR, status_code=429),
        actor_id=3,
        duration_ms=120,
    )
    assert set(event.as_fields()) == {
        "action",
        "outcome",
        "operation_id",
        "actor_id",
        "duration_ms",
        "error_category",
        "status_code",
    }
    assert event.as_fields()["status_code"] == 429
    assert event.as_fields()["error_category"] == "provider_error"


@pytest.mark.parametrize("value", [SECRET_VALUE, TASK_TEXT, "", UUID(int=0)])
def test_operation_id_rejects_strings_and_non_random_uuid(value: object) -> None:
    with pytest.raises(ValueError):
        AuditEvent(AuditAction.USER_ACTION, AuditOutcome.SUCCEEDED, value)


@pytest.mark.parametrize("field", ["actor_id", "duration_ms", "status_code"])
@pytest.mark.parametrize("value", [SECRET_VALUE, TASK_TEXT, True, -1])
def test_numeric_fields_reject_text_and_invalid_values(
    field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        AuditEvent(
            AuditAction.USER_ACTION, AuditOutcome.SUCCEEDED, uuid4(), **{field: value},
        )


def test_no_model_or_extra_text_field() -> None:
    with pytest.raises(TypeError):
        AuditEvent(
            AuditAction.MODEL_CALL, AuditOutcome.SUCCEEDED, uuid4(), model=SECRET_VALUE,
        )


def test_enum_fields_reject_untrusted_strings() -> None:
    with pytest.raises(TypeError):
        AuditEvent(SECRET_VALUE, AuditOutcome.SUCCEEDED, uuid4())
    with pytest.raises(TypeError):
        AuditEvent(AuditAction.MODEL_CALL, AuditOutcome.FAILED, uuid4(), error_category=TASK_TEXT)
