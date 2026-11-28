"""Test the Odoo risk-analysis binding with a mocked ORM and store, not a live database."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import ModuleType
import sys
from unittest.mock import MagicMock

import pytest

from adapters import odoo_risk_analysis as binding
from application.risk_summary import ProjectRiskSummary, SummaryAssignee, SummaryRow, SummaryStatus
from application.summary_store import SavedSummary
from application.task_read_map import ReadTool
from application.task_read_plan import ReadBudget
from application.task_result_release import ModelDraft, ReleaseStatus

_TASK_URL: str = "/odoo/project.task/9"
_CHECKED_AT: datetime = datetime(2026, 11, 27, tzinfo=UTC)
_TASK_ROW: dict[str, object] = {
    "id": 9, "name": "Demo task", "active": True, "state": "01_in_progress",
    "is_closed": False, "date_deadline": False, "user_ids": [7],
}


class AccessError(Exception):
    """Represent a mocked ORM access denial; AccessError() creates one."""


class MissingError(Exception):
    """Represent a mocked missing record; MissingError() creates one."""


class BrokenCompanies:
    """Raise when the current allowed company scope cannot be read."""

    @property
    def ids(self) -> list[int]:
        """Deny the company scope; ids raises AccessError."""
        raise AccessError()


@dataclass
class Store:
    """Return one immutable saved summary under the requested identity."""

    saved: SavedSummary | None
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    def get_summary(self, result_id: str, *, project_id: int, recipient_id: int,
                    authorize: Callable[[SavedSummary], bool]) -> SavedSummary | None:
        """Return the saved snapshot only for matching identity and authorization."""
        self.calls.append((result_id, project_id, recipient_id))
        snapshot: SavedSummary | None = self.saved
        if (snapshot is None or snapshot.result_id != result_id or snapshot.project_id != project_id
                or snapshot.recipient_id != recipient_id):
            return None
        return snapshot if authorize(snapshot) else None


@pytest.fixture
def odoo(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, dict[str, MagicMock]]:
    """Build the request and fresh recipient environments over one mocked ORM.

    Example: a test using odoo receives (env, fresh_env, models).
    """
    odoo_module: ModuleType = ModuleType("odoo")
    errors: ModuleType = ModuleType("odoo.exceptions")
    monkeypatch.setattr(odoo_module, "SUPERUSER_ID", 1, raising=False)
    monkeypatch.setattr(errors, "AccessError", AccessError, raising=False)
    monkeypatch.setattr(errors, "MissingError", MissingError, raising=False)
    monkeypatch.setitem(sys.modules, "odoo", odoo_module)
    monkeypatch.setitem(sys.modules, "odoo.exceptions", errors)
    models: dict[str, MagicMock] = {name: MagicMock() for name in ("project.project", "project.task", "res.users")}
    for model in models.values():
        model.fields_get.side_effect = lambda *, allfields, attributes: {field: {"type": "x"} for field in allfields}
    models["project.task"].search_read.return_value = [dict(_TASK_ROW)]
    models["res.users"].search_read.return_value = [{"id": 7, "name": "Demo user"}]
    env: MagicMock = _recipient(models)
    fresh: MagicMock = _recipient(models)
    return env, fresh, models


def _recipient(models: dict[str, MagicMock]) -> MagicMock:
    """Return a non-elevated internal environment bound to the mocked models."""
    env: MagicMock = MagicMock(su=False, uid=7)
    env.user.has_group.side_effect = lambda group: group == "base.group_user"
    env.__getitem__.side_effect = models.__getitem__
    env.registry.models = models
    env.cr.dbname = "pilot"
    env.companies.ids = [3, 1]
    return env


def _saved(*, task_id: int = 9, rows: tuple[SummaryRow, ...] | None = None,
           sources: tuple[int, ...] | None = (9,), recipient_id: int = 7, project_id: int = 4) -> SavedSummary:
    """Return a saved complete summary with one selected row for task 9."""
    selected: tuple[SummaryRow, ...] = rows if rows is not None else (
        SummaryRow(task_id, "Demo task", (SummaryAssignee(7, "Demo user"),), None, True, None, _TASK_URL, ()),
    )
    summary: ProjectRiskSummary = ProjectRiskSummary(
        SummaryStatus.COMPLETE, selected, ("RR-01",), (), sources, (),
    )
    return SavedSummary("result-1", "run-1", project_id, recipient_id, _CHECKED_AT, summary, "UTC")


def _prepare(env: MagicMock, store: Store) -> binding.PreparedRiskAnalysis:
    """Bind the selected saved result through the public entry point."""
    return binding.prepare_risk_analysis(
        env, store, project_id=4, task_id=9, result_id="result-1", budget=ReadBudget(),
    )


def test_prepare_binds_authorized_snapshot_selected_row_and_initial_card(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]],
) -> None:
    env, _, _ = odoo
    store: Store = Store(_saved())

    prepared: binding.PreparedRiskAnalysis = _prepare(env, store)

    assert prepared.status is binding.PrepareStatus.READY
    assert prepared.snapshot == store.saved
    assert prepared.row == store.saved.summary.rows[0]
    assert prepared.run is not None and prepared.gate is not None
    assert prepared.run.context is not None
    assert len(prepared.run.context.observations) == 1
    assert prepared.run.context.observations[0].step.tool is ReadTool.CARD
    assert store.calls == [("result-1", 4, 7)]


@pytest.mark.parametrize("attribute", ["su", "portal"])
def test_prepare_denies_elevated_or_portal_recipient(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]], attribute: str,
) -> None:
    env, _, _ = odoo
    store: Store = Store(_saved())
    if attribute == "su":
        env.su = True
    else:
        env.user.has_group.side_effect = lambda _group: False

    assert _prepare(env, store).status is binding.PrepareStatus.UNAVAILABLE
    assert store.calls == []


@pytest.mark.parametrize("case", ["missing", "project", "recipient", "row"])
def test_prepare_denies_wrong_or_missing_scope(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]], case: str,
) -> None:
    env, _, _ = odoo
    saved: SavedSummary = _saved()
    if case == "missing":
        store: Store = Store(None)
    elif case == "project":
        store = Store(replace(saved, project_id=5))
    elif case == "recipient":
        store = Store(replace(saved, recipient_id=8))
    else:
        store = Store(_saved(rows=()))

    assert _prepare(env, store).status is binding.PrepareStatus.UNAVAILABLE


def test_prepare_denies_a_row_missing_from_saved_provenance(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]],
) -> None:
    env, _, _ = odoo
    store: Store = Store(_saved(sources=()))

    assert _prepare(env, store).status is binding.PrepareStatus.UNAVAILABLE


def test_prepare_denies_unreadable_company_scope(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]],
) -> None:
    env, _, _ = odoo
    store: Store = Store(_saved())
    env.companies = BrokenCompanies()

    assert _prepare(env, store).status is binding.PrepareStatus.UNAVAILABLE


def _open(recipient: MagicMock) -> AbstractContextManager[MagicMock]:
    """Return a context manager that yields the supplied fresh recipient."""
    @contextmanager
    def opened() -> Iterator[MagicMock]:
        yield recipient

    return opened()


def test_release_rechecks_saved_snapshot_and_card_in_fresh_transaction(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, fresh, _ = odoo
    store: Store = Store(_saved())
    opened: list[tuple[str, int, tuple[int, ...]]] = []

    def open_recipient(_env: object, expected_db: str, user_id: int,
                       company_ids: tuple[int, ...]) -> AbstractContextManager[MagicMock]:
        opened.append((expected_db, user_id, company_ids))
        return _open(fresh)

    monkeypatch.setattr(binding, "_recipient_environment", open_recipient)
    prepared: binding.PreparedRiskAnalysis = _prepare(env, store)
    assert prepared.status is binding.PrepareStatus.READY and prepared.run is not None
    assert prepared.gate is not None

    outcome, released = prepared.gate.check(ModelDraft("Text", (_TASK_URL,)), prepared.run.context)

    assert outcome is ReleaseStatus.RELEASED
    assert released is not None and released.links == (_TASK_URL,)
    assert opened == [("pilot", 7, (3, 1))]
    assert store.calls == [("result-1", 4, 7), ("result-1", 4, 7)]


def test_release_blocks_a_changed_saved_snapshot(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, fresh, _ = odoo
    store: Store = Store(_saved())
    monkeypatch.setattr(binding, "_recipient_environment", lambda *_args: _open(fresh))
    prepared: binding.PreparedRiskAnalysis = _prepare(env, store)
    assert prepared.run is not None and prepared.gate is not None
    store.saved = replace(store.saved, checked_at=_CHECKED_AT + timedelta(minutes=1))

    outcome, released = prepared.gate.check(ModelDraft("Text", (_TASK_URL,)), prepared.run.context)

    assert outcome is ReleaseStatus.UNAVAILABLE
    assert released is None


def test_release_blocks_revoked_scope(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, fresh, _ = odoo
    store: Store = Store(_saved())
    monkeypatch.setattr(binding, "_recipient_environment", lambda *_args: _open(fresh))
    prepared: binding.PreparedRiskAnalysis = _prepare(env, store)
    assert prepared.run is not None and prepared.gate is not None
    store.saved = None

    outcome, released = prepared.gate.check(ModelDraft("Text", (_TASK_URL,)), prepared.run.context)

    assert outcome is ReleaseStatus.UNAVAILABLE
    assert released is None


def test_release_blocks_a_denied_fresh_environment(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, _ = odoo
    store: Store = Store(_saved())
    prepared: binding.PreparedRiskAnalysis = _prepare(env, store)
    assert prepared.run is not None and prepared.gate is not None

    def deny(*_args: object) -> AbstractContextManager[MagicMock]:
        raise binding.ExecutionDenied()

    monkeypatch.setattr(binding, "_recipient_environment", deny)
    outcome, released = prepared.gate.check(ModelDraft("Text", (_TASK_URL,)), prepared.run.context)

    assert outcome is ReleaseStatus.UNAVAILABLE
    assert released is None


def test_release_blocks_a_changed_card(
    odoo: tuple[MagicMock, MagicMock, dict[str, MagicMock]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, fresh, models = odoo
    store: Store = Store(_saved())
    monkeypatch.setattr(binding, "_recipient_environment", lambda *_args: _open(fresh))
    prepared: binding.PreparedRiskAnalysis = _prepare(env, store)
    assert prepared.run is not None and prepared.gate is not None
    models["project.task"].search_read.return_value = [{**_TASK_ROW, "name": "Changed task"}]

    outcome, released = prepared.gate.check(ModelDraft("Text", (_TASK_URL,)), prepared.run.context)

    assert outcome is ReleaseStatus.UNAVAILABLE
    assert released is None
