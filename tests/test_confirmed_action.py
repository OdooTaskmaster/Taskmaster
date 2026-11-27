"""Attack confirmation/version/authorization gates using trusted-port doubles."""

import json
import logging
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from application.audit import LOGGER_NAME
from application.confirmed_action import (
    ApplyOutcome,
    Confirmation,
    ConfirmationDecision,
    ConfirmedActionGate,
    Proposal,
    ProposalRecord,
)


@pytest.fixture
def ports() -> tuple[MagicMock, MagicMock, MagicMock, Proposal]:
    """Build a confirmed v2 proposal and three port doubles; e.g. source, access, writer, proposal = ports."""
    proposal: Proposal = Proposal("p1", 2, 3, 9, 8, "post_comment", "Exact approved text")
    source: MagicMock = MagicMock()
    access: MagicMock = MagicMock()
    writer: MagicMock = MagicMock()
    source.load.return_value = ProposalRecord(proposal, Confirmation(7, proposal, ConfirmationDecision.CONFIRMED))
    access.can_apply.return_value = True
    writer.apply.return_value = None
    return source, access, writer, proposal


def gate(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> ConfirmedActionGate:
    """Bind a server-owned scope; e.g. gate(ports).apply('p1', expected_version=2)."""
    source, access, writer, _ = ports
    return ConfirmedActionGate(source, access, writer, actor_id=7, project_id=3, task_id=9,
                               allowed_actions=frozenset({"post_comment"}))


def test_exact_confirmed_action_written_once(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> None:
    source, access, writer, proposal = ports
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.APPLIED
    access.can_apply.assert_called_once_with(7, proposal)
    writer.apply.assert_called_once_with(7, proposal)
    assert writer.apply.call_args.args[1] is proposal
    assert source.load.call_count == 2
    for call in source.load.call_args_list:
        assert call.args == ("p1",)
        assert call.kwargs == {"actor_id": 7, "project_id": 3, "task_id": 9}


@pytest.mark.parametrize("decision", [None, ConfirmationDecision.CANCELLED, ConfirmationDecision.REJECTED])
def test_absent_cancelled_rejected_never_authorize_or_write(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], decision: ConfirmationDecision | None,
) -> None:
    source, access, writer, proposal = ports
    confirmation: Confirmation | None = None if decision is None else Confirmation(7, proposal, decision)
    source.load.return_value = ProposalRecord(proposal, confirmation)
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    access.can_apply.assert_not_called()
    writer.apply.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("proposal_id", "other"), ("version", 1), ("project_id", 4), ("task_id", 10),
    ("recipient_id", 11), ("action", "delete_task"), ("payload", "Edited text"),
])
def test_any_edit_requires_new_confirmation(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], field: str, value: object,
) -> None:
    source, access, writer, proposal = ports
    source.load.return_value = ProposalRecord(replace(proposal, **{field: value}), source.load.return_value.confirmation)
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    access.can_apply.assert_not_called()
    writer.apply.assert_not_called()


@pytest.mark.parametrize("field,value", [("proposal_id", "other"), ("project_id", 4), ("task_id", 10), ("action", "delete_task")])
def test_matching_confirmation_cannot_override_bound_scope_or_allowlist(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], field: str, value: object,
) -> None:
    source, access, writer, proposal = ports
    changed: Proposal = replace(proposal, **{field: value})
    source.load.return_value = ProposalRecord(changed, Confirmation(7, changed, ConfirmationDecision.CONFIRMED))
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    access.can_apply.assert_not_called()
    writer.apply.assert_not_called()


def test_other_actor_cannot_supply_confirmation(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> None:
    source, access, writer, proposal = ports
    source.load.return_value = ProposalRecord(proposal, Confirmation(8, proposal, ConfirmationDecision.CONFIRMED))
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    access.can_apply.assert_not_called()
    writer.apply.assert_not_called()


@pytest.mark.parametrize("permission", [False, None, 1, "yes"])
def test_current_rights_require_true_not_truthiness(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], permission: object,
) -> None:
    source, access, writer, _ = ports
    access.can_apply.return_value = permission
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    assert source.load.call_count == 1
    writer.apply.assert_not_called()


def test_rights_rechecked_on_each_call(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> None:
    _, access, writer, _ = ports
    service: ConfirmedActionGate = gate(ports)
    assert service.apply("p1", expected_version=2) is ApplyOutcome.APPLIED
    access.can_apply.return_value = False
    assert service.apply("p1", expected_version=2) is ApplyOutcome.DENIED
    assert access.can_apply.call_count == 2
    assert writer.apply.call_count == 1


@pytest.mark.parametrize("change", ["cancel", "edit", "missing", "actor"])
def test_change_during_authorization_blocks_write(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], change: str,
) -> None:
    source, _, writer, proposal = ports
    initial: ProposalRecord = source.load.return_value
    changed: Proposal = replace(proposal, payload="Changed during rights check")
    latest: ProposalRecord | None = {
        "cancel": ProposalRecord(proposal, Confirmation(7, proposal, ConfirmationDecision.CANCELLED)),
        "edit": ProposalRecord(changed, Confirmation(7, changed, ConfirmationDecision.CONFIRMED)),
        "missing": None,
        "actor": ProposalRecord(proposal, Confirmation(8, proposal, ConfirmationDecision.CONFIRMED)),
    }[change]
    source.load.side_effect = [initial, latest]
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    writer.apply.assert_not_called()


@pytest.mark.parametrize("record", [None, "raw record", {}, True])
def test_missing_or_untyped_store_record_fails_closed(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], record: object,
) -> None:
    source, access, writer, _ = ports
    source.load.return_value = record
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    access.can_apply.assert_not_called()
    writer.apply.assert_not_called()


@pytest.mark.parametrize("port", ["source", "access", "writer", "second_load"])
def test_dependency_failures_do_not_retry_or_expose_details(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], port: str, caplog: pytest.LogCaptureFixture,
) -> None:
    source, access, writer, proposal = ports
    error: RuntimeError = RuntimeError("SECRET raw source detail " + proposal.payload)
    if port == "source":
        source.load.side_effect = error
    elif port == "second_load":
        source.load.side_effect = [source.load.return_value, error]
    elif port == "access":
        access.can_apply.side_effect = error
    else:
        writer.apply.side_effect = error
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        outcome: ApplyOutcome = gate(ports).apply("p1", expected_version=2)
    assert outcome is ApplyOutcome.FAILED
    assert writer.apply.call_count == (1 if port == "writer" else 0)
    assert "SECRET" not in caplog.text and proposal.payload not in caplog.text
    assert '"error_category": "unexpected"' in caplog.text


@pytest.mark.parametrize("version", [True, 0, -1, "2", 2.0, None])
def test_invalid_request_version_precedes_source(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], version: object,
) -> None:
    source, access, writer, _ = ports
    with pytest.raises(ValueError):
        gate(ports).apply("p1", expected_version=version)
    source.load.assert_not_called()
    access.can_apply.assert_not_called()
    writer.apply.assert_not_called()


@pytest.mark.parametrize("proposal_id", [None, "", " \t", 3, True])
def test_invalid_request_identity_precedes_source(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], proposal_id: object,
) -> None:
    source, _, writer, _ = ports
    with pytest.raises(ValueError):
        gate(ports).apply(proposal_id, expected_version=2)
    source.load.assert_not_called()
    writer.apply.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 0), ("project_id", False), ("task_id", -1),
    ("recipient_id", None), ("action", ""), ("payload", "  "), ("payload", {}), ("proposal_id", ""),
])
def test_invalid_proposal_cannot_be_constructed(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(ports[3], **{field: value})


@pytest.mark.parametrize("kwargs", [
    {"actor_id": True}, {"project_id": 0}, {"task_id": None},
    {"allowed_actions": {"post_comment"}}, {"allowed_actions": frozenset({""})},
])
def test_invalid_server_config_never_calls_dependencies(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], kwargs: dict[str, object],
) -> None:
    source, access, writer, _ = ports
    config: dict[str, object] = {"actor_id": 7, "project_id": 3, "task_id": 9,
                                 "allowed_actions": frozenset({"post_comment"})}
    config.update(kwargs)
    with pytest.raises(ValueError):
        ConfirmedActionGate(source, access, writer, **config)
    assert not source.mock_calls and not access.mock_calls and not writer.mock_calls


@pytest.mark.parametrize("port,method", [(0, "load"), (1, "can_apply"), (2, "apply")])
@pytest.mark.parametrize("invalid", [None, object(), "missing_method", "noncallable_method"])
def test_invalid_port_is_rejected_before_any_dependency_call(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], port: int, method: str, invalid: object,
) -> None:
    dependencies: list[object] = list(ports[:3])
    if isinstance(invalid, str):
        broken: MagicMock = MagicMock(spec=[])
        if invalid == "noncallable_method":
            setattr(broken, method, 42)
        dependencies[port] = broken
    else:
        dependencies[port] = invalid
    with pytest.raises(ValueError, match="callable port method"):
        ConfirmedActionGate(*dependencies, actor_id=7, project_id=3, task_id=9,
                            allowed_actions=frozenset({"post_comment"}))
    for dependency in ports[:3]:
        assert not dependency.mock_calls


@pytest.mark.parametrize("argument", ["actor_id", "project_id", "task_id", "recipient_id", "payload", "confirmation"])
def test_request_cannot_override_scope_or_confirmation(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], argument: str,
) -> None:
    source, _, writer, _ = ports
    with pytest.raises(TypeError):
        gate(ports).apply("p1", expected_version=2, **{argument: "forged"})
    source.load.assert_not_called()
    writer.apply.assert_not_called()


def test_stale_ui_version_and_empty_action_policy_deny(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> None:
    source, access, writer, _ = ports
    assert gate(ports).apply("p1", expected_version=1) is ApplyOutcome.DENIED
    disabled: ConfirmedActionGate = ConfirmedActionGate(
        source, access, writer, actor_id=7, project_id=3, task_id=9, allowed_actions=frozenset(),
    )
    assert disabled.apply("p1", expected_version=2) is ApplyOutcome.DENIED
    access.can_apply.assert_not_called()
    writer.apply.assert_not_called()


def test_no_implicit_idempotency_or_payload_transformation(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> None:
    source, _, writer, proposal = ports
    exact: Proposal = replace(proposal, payload=" <b>Final & exact</b>\n ")
    source.load.return_value = ProposalRecord(exact, Confirmation(7, exact, ConfirmationDecision.CONFIRMED))
    service: ConfirmedActionGate = gate(ports)
    assert service.apply("p1", expected_version=2) is ApplyOutcome.APPLIED
    assert service.apply("p1", expected_version=2) is ApplyOutcome.APPLIED
    assert writer.apply.call_count == 2
    assert all(call.args == (7, exact) for call in writer.apply.call_args_list)
    assert exact.payload not in repr(exact)


@pytest.mark.parametrize("outcome", list(ApplyOutcome))
def test_audit_outcomes_are_safe_and_distinguish_errors(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], outcome: ApplyOutcome,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, _, _, proposal = ports
    if outcome is ApplyOutcome.DENIED:
        source.load.return_value = None
    elif outcome is ApplyOutcome.FAILED:
        source.load.side_effect = RuntimeError("SECRET " + proposal.payload)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert gate(ports).apply("p1", expected_version=2) is outcome
    assert len(caplog.records) == 1
    fields: dict[str, object] = json.loads(caplog.records[0].getMessage().removeprefix("taskmaster.audit "))
    assert fields["outcome"] == ("succeeded" if outcome is ApplyOutcome.APPLIED else "failed")
    assert fields.get("error_category") == ("unexpected" if outcome is ApplyOutcome.FAILED else None)
    assert set(fields) == {"action", "outcome", "operation_id", "actor_id"} | (
        {"error_category"} if outcome is ApplyOutcome.FAILED else set()
    )
    assert "SECRET" not in caplog.text and proposal.payload not in caplog.text


@pytest.mark.parametrize("outcome", list(ApplyOutcome))
def test_broken_audit_handler_cannot_change_result_or_retry_write(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], outcome: ApplyOutcome,
) -> None:
    class BrokenHandler(logging.Handler):
        """Simulate a failing audit sink; e.g. BrokenHandler().emit(record) raises."""

        calls: int = 0

        def emit(self, record: logging.LogRecord) -> None:
            """Reject a log record without emitting its contents; e.g. handler.emit(record)."""
            self.calls += 1
            message: str = "Audit sink down"
            raise RuntimeError(message)

    source, _, writer, _ = ports
    if outcome is ApplyOutcome.DENIED:
        source.load.return_value = None
    elif outcome is ApplyOutcome.FAILED:
        source.load.side_effect = RuntimeError("Source failed")
    logger: logging.Logger = logging.getLogger(LOGGER_NAME)
    original_level: int = logger.level
    handler: BrokenHandler = BrokenHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        assert gate(ports).apply("p1", expected_version=2) is outcome
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        handler.close()
    assert handler.calls == 1
    assert writer.apply.call_count == (1 if outcome is ApplyOutcome.APPLIED else 0)


@pytest.mark.parametrize("field,value", [
    ("actor_id", True), ("actor_id", 0), ("proposal", {}), ("decision", "confirmed"), ("decision", None),
])
def test_confirmation_requires_typed_actor_snapshot_and_decision(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(Confirmation(7, ports[3], ConfirmationDecision.CONFIRMED), **{field: value})


@pytest.mark.parametrize("field,value", [("proposal", {}), ("confirmation", {}), ("confirmation", "confirmed")])
def test_record_rejects_untyped_nested_objects(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(ProposalRecord(ports[3], None), **{field: value})


@pytest.mark.parametrize("latest", [{}, "record", True])
def test_second_load_with_untyped_record_denies(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], latest: object,
) -> None:
    source, _, writer, _ = ports
    source.load.side_effect = [source.load.return_value, latest]
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.DENIED
    writer.apply.assert_not_called()


def test_first_version_can_be_confirmed(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> None:
    source, _, writer, proposal = ports
    first: Proposal = replace(proposal, version=1)
    source.load.return_value = ProposalRecord(first, Confirmation(7, first, ConfirmationDecision.CONFIRMED))
    assert gate(ports).apply("p1", expected_version=1) is ApplyOutcome.APPLIED
    writer.apply.assert_called_once_with(7, first)


def test_nested_snapshot_subclasses_are_rejected(ports: tuple[MagicMock, MagicMock, MagicMock, Proposal]) -> None:
    class DerivedProposal(Proposal):
        """Represent an unsupported snapshot subtype; e.g. DerivedProposal(...) is not a trusted Proposal."""

    class DerivedConfirmation(Confirmation):
        """Represent an unsupported decision subtype; e.g. DerivedConfirmation(...) is not trusted."""

    proposal: Proposal = ports[3]
    derived: DerivedProposal = DerivedProposal("p1", 2, 3, 9, 8, "post_comment", "Text")
    with pytest.raises(ValueError):
        Confirmation(7, derived, ConfirmationDecision.CONFIRMED)
    with pytest.raises(ValueError):
        ProposalRecord(derived, None)
    with pytest.raises(ValueError):
        ProposalRecord(proposal, DerivedConfirmation(7, proposal, ConfirmationDecision.CONFIRMED))


@pytest.mark.parametrize("text", ["\u200b", "\u200b\u200b", "\u2060"])
def test_opaque_payload_preserves_format_characters(
    ports: tuple[MagicMock, MagicMock, MagicMock, Proposal], text: str,
) -> None:
    source, _, writer, proposal = ports
    exact: Proposal = replace(proposal, payload=text)
    source.load.return_value = ProposalRecord(exact, Confirmation(7, exact, ConfirmationDecision.CONFIRMED))
    assert gate(ports).apply("p1", expected_version=2) is ApplyOutcome.APPLIED
    assert writer.apply.call_args.args[1].payload == text
