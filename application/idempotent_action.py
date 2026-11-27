"""Apply a confirmed snapshot using one authoritative, transactional receipt."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from .confirmed_action import Proposal


@dataclass(frozen=True, slots=True)
class AppliedAction:
    """Identify a write result without its contents; e.g. AppliedAction('42')."""

    reference: str

    def __post_init__(self) -> None:
        """Require a nonempty reference; AppliedAction('42') is valid."""
        if type(self.reference) is not str or not self.reference.strip():
            raise ValueError("A non-empty application reference is required.")


def idempotency_key(proposal: Proposal) -> str:
    """Derive a per-database key for the exact snapshot, regardless of confirming actor.

    The gate independently checks the actor. Example: idempotency_key(proposal)
    returns a stable 'sha256:...' string without the original content.
    """
    if type(proposal) is not Proposal:
        raise ValueError("A typed proposal is required.")
    fields: tuple[str, ...] = (
        proposal.proposal_id, str(proposal.version), str(proposal.project_id), str(proposal.task_id),
        str(proposal.recipient_id), proposal.action, proposal.payload,
    )
    canonical: str = "".join(f"{len(value)}:{value}" for value in fields)
    return f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}"


class ReconcilingChangeWriter(Protocol):
    """Return the authoritative write receipt for one confirmed snapshot."""

    def apply(self, actor_id: int, proposal: Proposal, *, key: str) -> AppliedAction:
        """Write once or reconcile; e.g. writer.apply(7, proposal, key=key) returns AppliedAction('42')."""
        ...


class IdempotentChangeWriter:
    """Adapt an authoritative writer for ConfirmedActionGate, not for direct HTTP use."""

    def __init__(self, writer: ReconcilingChangeWriter) -> None:
        """Bind a trusted writer; IdempotentChangeWriter(writer) exposes apply."""
        if not callable(getattr(writer, "apply", None)):
            raise ValueError("A callable writer is required.")
        self._writer: ReconcilingChangeWriter = writer

    def apply(self, actor_id: int, proposal: Proposal) -> AppliedAction:
        """Return the authoritative reference; e.g. apply(7, proposal) returns AppliedAction('42')."""
        if type(actor_id) is not int or actor_id <= 0:
            raise ValueError("A positive actor identity is required.")
        result: AppliedAction = self._writer.apply(actor_id, proposal, key=idempotency_key(proposal))
        if type(result) is not AppliedAction:
            raise ValueError("A typed applied action is required.")
        return result
