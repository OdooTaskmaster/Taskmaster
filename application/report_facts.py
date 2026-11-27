"""Assemble report evidence without treating missing coverage as no activity."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class ReportSection(StrEnum):
    """Identify the independent report sources, e.g. ReportSection.CREATED."""

    CREATED = "created"
    COMPLETED = "completed"
    WORKLOG = "worklog"
    CURRENT = "current"
    RISKS = "risks"
    PLAN = "plan"


class FactCoverage(StrEnum):
    """Describe a checked, incomplete, unavailable or inapplicable section."""

    CHECKED = "checked"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class ReportStatus(StrEnum):
    """Distinguish confirmed emptiness from incomplete evidence or denied scope."""

    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    """Bind inclusive project dates of up to 90 days; e.g. ReportPeriod(day, day)."""

    start: date
    end: date

    def __post_init__(self) -> None:
        """Reject invalid or unbounded dates; ReportPeriod(day, day) is valid."""
        if (type(self.start) is not date or type(self.end) is not date
                or self.start == date.min or self.end == date.max
                or not 0 <= (self.end - self.start).days < 90):
            raise ValueError("An inclusive report period of 1 to 90 days is required.")


@dataclass(frozen=True, slots=True)
class ReportFact:
    """Link an observed fact to a task and server-built source references.

    A datetime is a precise observation; a date is only a work day. Example:
    ReportFact(day, 9, ('/odoo/account.analytic.line/5',), quantity=Decimal(2), unit_id=1).
    """

    when: date | datetime
    task_id: int
    sources: tuple[str, ...]
    current_state: str | None = None
    quantity: Decimal | None = None
    unit_id: int | None = None
    risk_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SectionFacts:
    """Carry source coverage and observed rows, e.g. SectionFacts(FactCoverage.CHECKED)."""

    coverage: FactCoverage
    facts: tuple[ReportFact, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportFacts:
    """Return the authorized report, with independent status for dated work.

    Example: an EMPTY work_status and PARTIAL status mean checked empty work
    and a gap elsewhere, such as a missing plan. Denied scope has no sections.
    """

    period: ReportPeriod
    status: ReportStatus
    work_status: ReportStatus
    sections: tuple[tuple[ReportSection, SectionFacts], ...]


def _status(sections: tuple[SectionFacts, ...]) -> ReportStatus:
    """Summarize coverage; _status((SectionFacts(FactCoverage.CHECKED),)) is EMPTY."""
    if any(section.coverage in (FactCoverage.INCOMPLETE, FactCoverage.UNAVAILABLE) for section in sections):
        return ReportStatus.PARTIAL
    return ReportStatus.COMPLETE if any(section.facts for section in sections) else ReportStatus.EMPTY


def assemble_report_facts(
    period: ReportPeriod, sections: Mapping[ReportSection, SectionFacts] | None,
) -> ReportFacts:
    """Assemble already scoped evidence; assemble_report_facts(period, None) denies it.

    Omitted sections represent coverage gaps. Work status counts dated evidence,
    not currently open tasks, risk snapshots or future plans.
    """
    if sections is None:
        return ReportFacts(period, ReportStatus.UNAVAILABLE, ReportStatus.UNAVAILABLE, ())
    ordered: tuple[tuple[ReportSection, SectionFacts], ...] = tuple(
        (kind, sections.get(kind, SectionFacts(FactCoverage.UNAVAILABLE))) for kind in ReportSection
    )
    work: tuple[SectionFacts, ...] = tuple(data for kind, data in ordered if kind in (
        ReportSection.CREATED, ReportSection.COMPLETED, ReportSection.WORKLOG,
    ))
    return ReportFacts(period, _status(tuple(data for _, data in ordered)), _status(work), ordered)
