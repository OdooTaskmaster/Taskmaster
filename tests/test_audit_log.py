"""Verify audit events carry safe metadata only."""

import json
import logging

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


def _payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.getMessage().removeprefix(AUDIT_PREFIX))
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


def test_three_actions_share_one_operation_id(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    log_event(AuditEvent(AuditAction.USER_ACTION, AuditOutcome.SUCCEEDED, "op-42", actor_id=7))
    log_event(
        AuditEvent(
            AuditAction.AGENT_RUN,
            AuditOutcome.SUCCEEDED,
            "op-42",
            model="z-ai/glm-5.3-flash",
        ),
    )
    log_event(failure_event(AuditAction.MODEL_CALL, "op-42", ModelFailure(ErrorCategory.TIMEOUT)))

    payloads = _payloads(caplog)

    assert [payload["action"] for payload in payloads] == [
        "user_action",
        "agent_run",
        "model_call",
    ]
    assert {payload["operation_id"] for payload in payloads} == {"op-42"}
    assert [payload["outcome"] for payload in payloads] == ["succeeded", "succeeded", "failed"]
    assert payloads[2]["error_category"] == "timeout"


def test_fields_are_allow_listed() -> None:
    event = AuditEvent(
        AuditAction.MODEL_CALL,
        AuditOutcome.FAILED,
        "op-1",
        actor_id=3,
        model="z-ai/glm-5.3-flash",
        duration_ms=120,
        error_category=ErrorCategory.PROVIDER_ERROR,
        status_code=500,
    )

    assert set(event.as_fields()) == {
        "action",
        "outcome",
        "operation_id",
        "actor_id",
        "model",
        "duration_ms",
        "error_category",
        "status_code",
    }


def test_exception_message_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    error = RuntimeError(f"{SECRET_VALUE} {TASK_TEXT}")

    log_event(failure_event(AuditAction.AGENT_RUN, "op-9", error))

    assert SECRET_VALUE not in caplog.text
    assert TASK_TEXT not in caplog.text
    assert "RuntimeError" not in caplog.text
    payload = _payloads(caplog)[0]
    assert payload["error_category"] == "unexpected"
    assert "status_code" not in payload


def test_model_failure_keeps_only_category_and_status(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    error = ModelFailure(ErrorCategory.PROVIDER_ERROR, status_code=429)

    log_event(failure_event(AuditAction.MODEL_CALL, "op-5", error))

    payload = _payloads(caplog)[0]
    assert payload["error_category"] == "provider_error"
    assert payload["status_code"] == 429
    assert "HTTP" not in caplog.text


def test_operation_id_is_required() -> None:
    with pytest.raises(ValueError):
        AuditEvent(AuditAction.USER_ACTION, AuditOutcome.SUCCEEDED, "   ")


def test_text_fields_are_cleaned_and_limited() -> None:
    event = AuditEvent(
        AuditAction.USER_ACTION,
        AuditOutcome.SUCCEEDED,
        "  op\n42  ",
        model="a" * 300,
    )

    fields = event.as_fields()

    assert fields["operation_id"] == "op 42"
    assert len(str(fields["model"])) == 120


def test_event_has_no_extra_fields() -> None:
    event = AuditEvent(AuditAction.USER_ACTION, AuditOutcome.SUCCEEDED, "op-1")

    assert not hasattr(event, "notes")
    assert "notes" not in event.as_fields()
