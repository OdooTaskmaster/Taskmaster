"""Withhold all generated text until every context receipt is reauthorized."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .reading_plan import ContextItem, ReadingRun, SourceRef


class ContextVerifier(Protocol):
    """Recheck complete receipts in a fresh authorization transaction; e.g. verifier.check(items)."""

    def check(self, items: tuple[ContextItem, ...]) -> bool:
        """Reauthorize and compare all used values, not only citations; e.g. check(run.items)."""
        ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """Carry plain generated text and reference IDs, never model URLs; e.g. Candidate('Summary', (ref,))."""

    text: str
    citations: tuple[SourceRef, ...]


class GateStatus(StrEnum):
    """Separate valid, denied, invalid and failed outputs; e.g. GateStatus.DENIED."""

    AVAILABLE = "available"
    DENIED = "denied"
    INVALID = "invalid"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CheckedAnswer:
    """Retain authorized plain text, server links and all provenance; e.g. CheckedAnswer(text, links, items)."""

    text: str
    links: tuple[str, ...]
    provenance: tuple[ContextItem, ...]


class SourceGate:
    """Bind final validation to a trusted run and fresh verifier; e.g. SourceGate(run, verifier).validate(candidate).

    All initial facts and subsequent reads supplied to the model must come from
    this run. Persist provenance with a checked answer; do not treat saved text
    as permanently authorized. A new presentation needs a new current-rights check.
    Text is plain content: render escaped, not as HTML/Markdown/autolinked URLs.
    """

    def __init__(self, run: ReadingRun, verifier: ContextVerifier, *, max_text_chars: int = 8000) -> None:
        """Bind server-owned provenance and output limit; e.g. SourceGate(run, verifier)."""
        if (type(max_text_chars) is not int or max_text_chars <= 0
                or not callable(getattr(verifier, "check", None))):
            message: str = "A verifier and positive output limit required"
            raise ValueError(message)
        self._run: ReadingRun = run
        self._verifier: ContextVerifier = verifier
        self._max_text: int = max_text_chars

    def validate(self, candidate: Candidate) -> tuple[GateStatus, CheckedAnswer | None]:
        """Release no text on unknown references or revoked unquoted data; e.g. gate.validate(candidate)."""
        if (type(candidate) is not Candidate or type(candidate.text) is not str or not candidate.text.strip()
                or len(candidate.text) > self._max_text or type(candidate.citations) is not tuple
                or any(type(ref) is not SourceRef for ref in candidate.citations)
                or len(set(candidate.citations)) != len(candidate.citations)
                or re.search(r"https?://|/odoo/|/mail/|mail\.message:", candidate.text, re.IGNORECASE)):
            return GateStatus.INVALID, None
        items: tuple[ContextItem, ...] = self._run.items
        known: set[SourceRef] = {ref for item in items for ref in item.sources}
        if not items or not set(candidate.citations).issubset(known):
            return GateStatus.INVALID, None
        if self._run.expired():
            return GateStatus.EXPIRED, None
        try:
            if self._verifier.check(items) is not True:
                return GateStatus.DENIED, None
            if self._run.expired():
                return GateStatus.EXPIRED, None
            return GateStatus.AVAILABLE, CheckedAnswer(
                candidate.text, tuple(ref.link() for ref in candidate.citations), items,
            )
        except Exception:  # noqa: BLE001 -- Never release partial text or source diagnostics.
            return GateStatus.FAILED, None
