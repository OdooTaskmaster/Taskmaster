"""Gate a trusted proposed change on exact confirmation and current authorization."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .audit import AuditAction, AuditEvent, AuditOutcome, log_event
from .errors import ErrorCategory


def _positive_id(value: int) -> None:
    """Reject non-positive or non-integer IDs; e.g. _positive_id(7) returns None."""
    if type(value) is not int or value <= 0:
        message: str = "A positive integer identity or version is required."
        raise ValueError(message)


def _nonempty(value: str) -> None:
    """Reject blank or non-string values without echoing them; e.g. _nonempty('v1')."""
    if type(value) is not str or not value.strip():
        message: str = "A non-empty string is required."
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Proposal:
    """Bind an immutable action and payload to a version, scope and recipient.

    payload is an opaque string owned by the action-specific adapter, not a
    generic ORM command. No normalization or extra fields are added on apply.
    Example: Proposal('p1', 1, 3, 9, 8, 'post_comment', 'Approved text').
    """

    proposal_id: str
    version: int
    project_id: int
    task_id: int
    recipient_id: int
    action: str
    payload: str = field(repr=False)

    def __post_init__(self) -> None:
        """Validate identity and content types; Proposal(...) raises ValueError on invalid input."""
        for value in (self.version, self.project_id, self.task_id, self.recipient_id):
            _positive_id(value)
        for text in (self.proposal_id, self.action, self.payload):
            _nonempty(text)


class ConfirmationDecision(StrEnum):
    """Name an explicit stored user decision; e.g. ConfirmationDecision.CONFIRMED."""

    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Confirmation:
    """Bind a user's explicit decision to the entire displayed proposal snapshot.

    Only a trusted server-side store supplies this object, never model output
    or an unverified request body. Example: Confirmation(7, proposal, decision).
    """

    actor_id: int
    proposal: Proposal
    decision: ConfirmationDecision

    def __post_init__(self) -> None:
        """Validate the actor and typed snapshot; Confirmation(...) rejects untyped decisions."""
        _positive_id(self.actor_id)
        if type(self.proposal) is not Proposal or type(self.decision) is not ConfirmationDecision:
            message: str = "A typed proposal and decision are required."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    """Carry the current proposal and its latest stored decision, possibly absent.

    Example: ProposalRecord(proposal, None) represents an unconfirmed proposal.
    """

    proposal: Proposal
    confirmation: Confirmation | None

    def __post_init__(self) -> None:
        """Validate stored object types; ProposalRecord(proposal, None) is allowed."""
        if type(self.proposal) is not Proposal or (
            self.confirmation is not None and type(self.confirmation) is not Confirmation
        ):
            message: str = "A typed proposal record is required."
            raise ValueError(message)


class ProposalSource(Protocol):
    """Load authoritative snapshots, never accept confirmation from the model.

    The implementation must return the latest decision including cancellation;
    actor/project/task scope is server supplied. Example: source.load('p1',
    actor_id=7, project_id=3, task_id=9) returns ProposalRecord or None.
    """

    def load(self, proposal_id: str, *, actor_id: int, project_id: int, task_id: int) -> ProposalRecord | None:
        """Return the current scoped record or None; e.g. source.load('p1', actor_id=7, project_id=3, task_id=9)."""
        ...


class ChangeAccess(Protocol):
    """Check current role, membership, company, record, field and recipient rights.

    The implementation must use the actor's ordinary Odoo rights and check the
    action's exact payload/target and recipient, not merely task visibility.
    Example: access.can_apply(7, proposal) returns a strict bool.
    """

    def can_apply(self, actor_id: int, proposal: Proposal) -> bool:
        """Authorize this exact change now; e.g. access.can_apply(7, proposal)."""
        ...


class ChangeWriter(Protocol):
    """Apply only the supplied immutable change under ordinary actor rights.

    A real adapter must enforce Odoo write rights at the actual write boundary;
    the application check cannot grant rights or freeze another transaction.
    The gate does not retry; a writer may reconcile a repeat using its receipt.
    Example: writer.apply(7, proposal) returns None or an internal receipt on success.
    """

    def apply(self, actor_id: int, proposal: Proposal) -> object:
        """Apply exactly the confirmed payload or raise; e.g. writer.apply(7, proposal)."""
        ...


class ApplyOutcome(StrEnum):
    """Report a safe result without stored content; e.g. ApplyOutcome.DENIED."""

    APPLIED = "applied"
    DENIED = "denied"
    FAILED = "failed"


class ConfirmedActionGate:
    """Check trusted confirmation before handing one exact change to a writer.

    Bind actor/project/task and allowed actions on the server. Construction
    does not confirm anything. Example: gate.apply('p1', expected_version=2).
    Dependencies are ports; no storage, HTTP endpoint or real write adapter is
    installed by this module. Concurrent atomic apply belongs to #31.
    """

    def __init__(
        self, source: ProposalSource, access: ChangeAccess, writer: ChangeWriter, *,
        actor_id: int, project_id: int, task_id: int, allowed_actions: frozenset[str],
    ) -> None:
        """Bind trusted dependencies and scope; e.g. ConfirmedActionGate(source, access, writer,

        actor_id=7, project_id=3, task_id=9, allowed_actions=frozenset({'post_comment'})).
        Invalid server configuration raises ValueError before any dependency call.
        """
        for value in (actor_id, project_id, task_id):
            _positive_id(value)
        if type(allowed_actions) is not frozenset:
            message: str = "An immutable action allow-list is required."
            raise ValueError(message)
        for action in allowed_actions:
            _nonempty(action)
        for dependency, method in ((source, "load"), (access, "can_apply"), (writer, "apply")):
            if not callable(getattr(dependency, method, None)):
                message: str = "A dependency with a callable port method is required."
                raise ValueError(message)  # noqa: TRY004 -- Keep one ValueError contract for invalid server configuration.
        self._source: ProposalSource = source
        self._access: ChangeAccess = access
        self._writer: ChangeWriter = writer
        self._actor_id: int = actor_id
        self._project_id: int = project_id
        self._task_id: int = task_id
        self._allowed_actions: frozenset[str] = allowed_actions

    def _load(self, proposal_id: str) -> ProposalRecord | None:
        """Read under bound scope; e.g. gate._load('p1') returns a trusted record or None."""
        return self._source.load(
            proposal_id, actor_id=self._actor_id, project_id=self._project_id, task_id=self._task_id,
        )

    def _matches(self, record: ProposalRecord | None, proposal_id: str, version: int) -> bool:
        """Match all immutable confirmation fields; e.g. gate._matches(record, 'p1', 2)."""
        if type(record) is not ProposalRecord:
            return False
        proposal: Proposal = record.proposal
        confirmation: Confirmation | None = record.confirmation
        return (
            proposal.proposal_id == proposal_id and proposal.version == version
            and proposal.project_id == self._project_id and proposal.task_id == self._task_id
            and proposal.action in self._allowed_actions and confirmation is not None
            and confirmation.actor_id == self._actor_id
            and confirmation.decision is ConfirmationDecision.CONFIRMED
            and confirmation.proposal == proposal
        )

    def apply(self, proposal_id: str, *, expected_version: int) -> ApplyOutcome:
        """Apply once in this call only after confirmation and a fresh rights check.

        Invalid request types raise ValueError before source access. Missing,
        cancelled, stale or forbidden proposals return DENIED with zero writes.
        Source/access/write errors return FAILED without raw diagnostics; a
        failed write may have taken effect, so do not retry automatically.
        Audit is best-effort: sink failures never change the action outcome.
        Example: gate.apply('p1', expected_version=2) returns ApplyOutcome.APPLIED.
        """
        _nonempty(proposal_id)
        _positive_id(expected_version)
        operation_id: UUID = uuid4()
        outcome: ApplyOutcome = ApplyOutcome.DENIED
        try:
            record: ProposalRecord | None = self._load(proposal_id)
            if self._matches(record, proposal_id, expected_version) and record is not None:
                permitted: bool = self._access.can_apply(self._actor_id, record.proposal)
                if permitted is True:
                    # Catch edits/cancellation during the rights check, not just stale UI versions.
                    latest: ProposalRecord | None = self._load(proposal_id)
                    if latest == record and self._matches(latest, proposal_id, expected_version):
                        self._writer.apply(self._actor_id, record.proposal)
                        outcome = ApplyOutcome.APPLIED
        except Exception:  # noqa: BLE001 -- Never expose source/write exception text or retry a write.
            outcome = ApplyOutcome.FAILED
        try:
            log_event(AuditEvent(
                AuditAction.USER_ACTION,
                AuditOutcome.SUCCEEDED if outcome is ApplyOutcome.APPLIED else AuditOutcome.FAILED,
                operation_id, actor_id=self._actor_id,
                error_category=ErrorCategory.UNEXPECTED if outcome is ApplyOutcome.FAILED else None,
            ))
        except Exception:  # noqa: BLE001 -- A broken audit sink must not suggest retrying a completed write.
            pass
        return outcome
