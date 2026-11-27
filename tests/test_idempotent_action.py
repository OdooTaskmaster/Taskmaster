"""Verify confirmation gates and the authoritative receipt boundary."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.odoo_action_receipt import OdooActionReceiptWriter
from application.confirmed_action import ApplyOutcome, Confirmation, ConfirmationDecision, ConfirmedActionGate, Proposal, ProposalRecord
from application.idempotent_action import AppliedAction, IdempotentChangeWriter, idempotency_key


@pytest.fixture
def proposal() -> Proposal:
    """Return a synthetic proposal; the fixture has version 2."""
    return Proposal("p1", 2, 3, 9, 8, "post_comment", "Synthetic text")


def test_snapshot_key_changes_with_version_and_payload(proposal: Proposal) -> None:
    """Bind a stable key to the full snapshot without exposing the payload."""
    key: str = idempotency_key(proposal)
    assert key == idempotency_key(replace(proposal))
    assert key != idempotency_key(replace(proposal, version=3))
    assert key != idempotency_key(replace(proposal, payload="Another synthetic text"))
    assert proposal.payload not in key


@pytest.mark.parametrize("reference", ["", " ", None, 5])
def test_receipt_reference_must_be_nonempty(reference: str) -> None:
    """Reject malformed result references instead of reporting success."""
    with pytest.raises(ValueError):
        AppliedAction(reference)


def test_gate_rechecks_authorization_before_reconciliation(proposal: Proposal) -> None:
    """Consult the authoritative writer as long as the exact confirmation and rights hold."""
    source: Mock = Mock()
    source.load.return_value = ProposalRecord(proposal, Confirmation(7, proposal, ConfirmationDecision.CONFIRMED))
    access: Mock = Mock()
    access.can_apply.return_value = True
    writer: Mock = Mock()
    writer.apply.return_value = AppliedAction("42")
    gate: ConfirmedActionGate = ConfirmedActionGate(
        source, access, IdempotentChangeWriter(writer), actor_id=7,
        project_id=3, task_id=9, allowed_actions=frozenset({"post_comment"}),
    )
    assert gate.apply("p1", expected_version=2) is ApplyOutcome.APPLIED
    assert gate.apply("p1", expected_version=2) is ApplyOutcome.APPLIED
    assert writer.apply.call_count == 2
    writer.apply.assert_called_with(7, proposal, key=idempotency_key(proposal))
    access.can_apply.return_value = False
    assert gate.apply("p1", expected_version=2) is ApplyOutcome.DENIED
    assert writer.apply.call_count == 2


def test_transaction_reuses_reference_and_never_calls_business_write_on_repeat(proposal: Proposal) -> None:
    """A cursor conflict resolves to the authoritative reference without another write."""
    cursor: Mock = Mock()
    from contextlib import nullcontext

    cursor.savepoint.side_effect = nullcontext
    cursor.fetchone.side_effect = [(4,), None, ("42",)]
    env: SimpleNamespace = SimpleNamespace(cr=cursor, uid=7, su=False)
    write: Mock = Mock(return_value="42")
    adapter: IdempotentChangeWriter = IdempotentChangeWriter(OdooActionReceiptWriter(env, write))
    assert adapter.apply(7, proposal) == adapter.apply(7, proposal) == AppliedAction("42")
    write.assert_called_once_with(env, proposal)


def test_transaction_fails_closed_for_invisible_receipt_and_wrong_actor(proposal: Proposal) -> None:
    """Never call the underlying writer if an existing receipt is unreadable or scope is wrong."""
    cursor: Mock = Mock()
    from contextlib import nullcontext

    cursor.savepoint.side_effect = nullcontext
    cursor.fetchone.side_effect = [None, None]
    write: Mock = Mock()
    writer: OdooActionReceiptWriter = OdooActionReceiptWriter(SimpleNamespace(cr=cursor, uid=7, su=False), write)
    with pytest.raises(PermissionError):
        writer.apply(8, proposal, key=idempotency_key(proposal))
    cursor.execute.assert_not_called()
    with pytest.raises(RuntimeError, match="could not be verified"):
        writer.apply(7, proposal, key=idempotency_key(proposal))
    write.assert_not_called()
