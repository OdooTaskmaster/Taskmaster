"""Project the dated evidence of one authorized risk row for analysis."""

from dataclasses import dataclass
from datetime import date, datetime

from .risk_summary import DeadlineDeviation, SummaryRow
from .summary_store import SavedSummary


@dataclass(frozen=True, slots=True)
class RiskSignal:
    """Describe why the saved check flagged a task without sending the rest of the summary."""

    checked_at: datetime
    project_timezone: str | None
    rules: tuple[str, ...]
    deadline_known: bool
    deadline: date | datetime | None
    deviation: DeadlineDeviation | None

    @classmethod
    def from_saved(cls, saved: SavedSummary, row: SummaryRow) -> "RiskSignal":
        """Project an authorized row; from_saved(snapshot, row) includes only dated evidence.

        The caller must reauthorize the same snapshot before releasing any result
        derived from this projection. A row outside the snapshot is rejected.
        """
        if row not in saved.summary.rows:
            raise ValueError("The risk row does not belong to the saved summary")
        return cls(saved.checked_at, saved.project_timezone, tuple(reason.rule_id for reason in row.reasons),
                   row.deadline_known, row.deadline, row.deviation)

    def model_data(self) -> dict[str, object]:
        """Return JSON-ready historical values; no current facts or links are inferred."""
        return {
            "checked_at": self.checked_at.isoformat(),
            "project_timezone": self.project_timezone,
            "rules": self.rules,
            "deadline_known": self.deadline_known,
            "deadline": None if self.deadline is None else self.deadline.isoformat(),
            "deviation": None if self.deviation is None else {
                "overdue": self.deviation.overdue, "days": self.deviation.days,
            },
        }
