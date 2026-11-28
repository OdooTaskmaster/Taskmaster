"""Validate a model result and recheck all its input evidence before release."""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from time import monotonic

from .audit import LOGGER_NAME
from .dialogue_provenance import Evidence, MAX_EVIDENCE
from .task_read_plan import ReadingContext, ReadObservation
from .task_read_tools import ReadStatus, TaskReadTools


@dataclass(frozen=True, slots=True)
class ModelDraft:
    """Carry untrusted plain text and citation keys; e.g. ModelDraft('Text', ())."""

    text: str
    citations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReleasedResult:
    """Carry inert text and separate server-built links ready for delivery."""

    text: str
    links: tuple[str, ...]


class ReleaseStatus(StrEnum):
    """Distinguish release, validation, access and operational outcomes."""

    RELEASED = "released"
    INVALID_RESULT = "invalid_result"
    UNAVAILABLE = "unavailable"
    TIME_LIMIT = "time_limit"
    FAILED = "failed"


class TaskResultRelease:
    """Apply the final check in one fresh recipient transaction per attempt.

    The server supplies a factory that opens a new transaction, with no sudo,
    for the same recipient, project and task as the original read. The factory
    must not reuse the original reader, transaction or any access cache. It
    must close the transaction on context exit. The gate does not publish,
    persist, or lock permissions against a later transaction's changes.
    """

    def __init__(
        self, fresh_tools: Callable[[], AbstractContextManager[TaskReadTools]], *,
        seconds: float = 2.0, clock: Callable[[], float] = monotonic,
    ) -> None:
        """Bind the fresh-read factory and a separate, positive deadline."""
        if type(seconds) not in (int, float) or not isfinite(seconds) or seconds <= 0:
            raise ValueError("Invalid release budget")
        self._fresh_tools: Callable[[], AbstractContextManager[TaskReadTools]] = fresh_tools
        self._seconds: float = seconds
        self._clock: Callable[[], float] = clock

    def check(
        self, draft: ModelDraft, context: ReadingContext | None, *, inherited: tuple[Evidence, ...] = (),
    ) -> tuple[ReleaseStatus, ReleasedResult | None]:
        """Release only against the server-held context actually supplied to the model.

        Compare the same narrow projections, including uncited fields, rows,
        and source-less verified empty selections. Besides revoked access,
        a changed projection blocks the old result conservatively. Call this
        just before every delivery; render text as plain text, not links or HTML.
        """
        status: ReleaseStatus
        released: ReleasedResult | None
        status, released = self._check(draft, context, inherited)
        logging.getLogger(LOGGER_NAME).info("task_result_release status=%s", status)
        return status, released

    def _check(
        self, draft: ModelDraft, context: ReadingContext | None, inherited: tuple[Evidence, ...],
    ) -> tuple[ReleaseStatus, ReleasedResult | None]:
        """Check the draft and context without exposing source or exception details."""
        deadline: float = self._clock() + self._seconds
        if (type(draft) is not ModelDraft or type(draft.text) is not str or not draft.text.strip()
                or "\x00" in draft.text or type(draft.citations) is not tuple):
            return ReleaseStatus.INVALID_RESULT, None
        try:
            draft.text.encode("utf-8")
        except UnicodeError:
            return ReleaseStatus.INVALID_RESULT, None
        if context is None or not context.observations:
            return ReleaseStatus.UNAVAILABLE, None

        known: dict[str, str] = dict(pair for item in context.observations for pair in item.citations)
        links: dict[str, None] = {}
        if len(draft.citations) > len(known):
            return ReleaseStatus.INVALID_RESULT, None
        for citation in draft.citations:
            if type(citation) is not str or citation not in known:
                return ReleaseStatus.INVALID_RESULT, None
            links[known[citation]] = None

        outcome: ReleaseStatus = self._verify(context, inherited, deadline)
        if outcome is not ReleaseStatus.RELEASED:
            return outcome, None
        return outcome, ReleasedResult(draft.text, tuple(links))

    def check_history(self, inherited: tuple[Evidence, ...], *, deadline: float | None = None) -> bool:
        """Recheck prior reply fingerprints with an optional shared monotonic deadline.

        Example: check_history(()) denies empty evidence.
        """
        local_deadline: float = self._clock() + self._seconds
        if deadline is not None:
            local_deadline = min(local_deadline, deadline)
        return bool(inherited) and self._verify(None, inherited, local_deadline) is ReleaseStatus.RELEASED

    def _verify(self, context: ReadingContext | None, inherited: tuple[Evidence, ...], deadline: float) -> ReleaseStatus:
        """Replay current and inherited reads in one fresh scope; return RELEASED only if all match."""
        if len(inherited) > MAX_EVIDENCE:
            return ReleaseStatus.INVALID_RESULT
        if self._clock() >= deadline:
            return ReleaseStatus.TIME_LIMIT
        outcome: ReleaseStatus = ReleaseStatus.FAILED
        try:
            with self._fresh_tools() as tools:
                seen: set[Evidence] = set()
                for item in context.observations if context is not None else ():
                    if self._clock() >= deadline:
                        outcome = ReleaseStatus.TIME_LIMIT
                        break
                    status, value = item.step.read(tools)
                    if status is not ReadStatus.AVAILABLE:
                        outcome = (ReleaseStatus.UNAVAILABLE if status is ReadStatus.UNAVAILABLE
                                   else ReleaseStatus.FAILED)
                        break
                    if ReadObservation.capture(item.step, value) != item:
                        outcome = ReleaseStatus.UNAVAILABLE
                        break
                    seen.add((item.step, sha256(item.encoded).hexdigest()))
                else:
                    outcome = ReleaseStatus.RELEASED
                    for step, digest in inherited:
                        if (step, digest) in seen:
                            continue
                        if self._clock() >= deadline:
                            outcome = ReleaseStatus.TIME_LIMIT
                            break
                        status, value = step.read(tools)
                        if status is not ReadStatus.AVAILABLE:
                            outcome = ReleaseStatus.UNAVAILABLE if status is ReadStatus.UNAVAILABLE else ReleaseStatus.FAILED
                            break
                        actual: ReadObservation | None = ReadObservation.capture(step, value)
                        if actual is None or sha256(actual.encoded).hexdigest() != digest:
                            outcome = ReleaseStatus.UNAVAILABLE
                            break
                        seen.add((step, digest))
        except Exception:  # noqa: BLE001
            return ReleaseStatus.FAILED
        if self._clock() >= deadline:
            return ReleaseStatus.TIME_LIMIT
        return outcome
