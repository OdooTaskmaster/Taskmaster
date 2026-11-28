from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

from application.audit import LOGGER_NAME
from application.dialogue_provenance import collect_evidence, decode_evidence, encode_evidence
from application.task_read_map import ReadScenario, ReadTool, TaskReadMap
from application.task_read_plan import ReadStep, TaskReadRun
from application.task_read_tools import (
    Comment, Comments, History, ReadStatus, TaskCard, TaskReadTools, Transition, Worklog, WorklogEntry, WorklogTotal,
)
from application.task_result_release import ModelDraft, ReleasedResult, ReleaseStatus, TaskResultRelease

DAY = date(2026, 11, 27)
TASK = "/odoo/project.task/9"
MESSAGE = "/mail/message/17"
LINE = "/odoo/account.analytic.line/43"
CARD = ReadStep(ReadTool.CARD)
COMMENTS = ReadStep(ReadTool.COMMENTS, DAY, DAY)
HISTORY = ReadStep(ReadTool.HISTORY, DAY, DAY)
WORKLOG = ReadStep(ReadTool.WORKLOG, DAY, DAY)
CARD_VALUE = TaskCard("Demo task", None, None, False, None, TASK)
DRAFT = ModelDraft("Uncited discussion informed this explanation.", (TASK,))


@dataclass
class Snapshot:
    calls: list[str]
    project_visible: bool
    comment_visible: bool
    card_fields_visible: bool

    def read_types(self):
        return ReadStatus.AVAILABLE, tuple(ReadTool)

    def card(self):
        self.calls.append("card")
        if not self.project_visible:
            return ReadStatus.UNAVAILABLE, None
        card = CARD_VALUE if self.card_fields_visible else TaskCard(None, None, None, False, None, TASK)
        return ReadStatus.AVAILABLE, card

    def comments(self, start, end, limit):
        self.calls.append("comments")
        visible = (Comment(DAY, "Synthetic detail", MESSAGE),) if self.comment_visible else ()
        return ReadStatus.AVAILABLE, Comments(visible, False, False)

    def history(self, start, end):
        self.calls.append("history")
        return ReadStatus.AVAILABLE, History((Transition(DAY, "stage", 1, 2, "mail.message:17"),), False)

    def worklog(self, start, end):
        self.calls.append("worklog")
        return ReadStatus.AVAILABLE, Worklog(
            (WorklogTotal(1, "Hours", Decimal("1.20")),),
            (WorklogEntry(DAY, Decimal("1.20"), 1, 1, (LINE,)),), False,
        )


@dataclass
class Store:
    project_visible: bool = True
    comment_visible: bool = True
    card_fields_visible: bool = True
    now: float = 0.0
    close_duration: float = 0.0
    fresh_calls: list[str] = field(default_factory=list)

    def snapshot(self, calls):
        return Snapshot(calls, self.project_visible, self.comment_visible, self.card_fields_visible)

    @contextmanager
    def fresh(self) -> Iterator[TaskReadTools]:
        self.fresh_calls.append("open")
        try:
            yield TaskReadTools(self.snapshot(self.fresh_calls))
        finally:
            self.fresh_calls.append("close")
            self.now += self.close_duration

    def clock(self):
        return self.now


def collect(store, plan=(COMMENTS,), initial=((CARD, CARD_VALUE),)):
    snapshot = store.snapshot([])
    tools = TaskReadTools(snapshot)
    reader_map = TaskReadMap(snapshot, scenario=ReadScenario.RISK_ANALYSIS)
    run = TaskReadRun(reader_map, tools, initial=initial, clock=store.clock)
    return run.execute(plan).context, tools


def test_success_rechecks_initial_and_uncited_evidence_in_one_fresh_read_session():
    store = Store()
    context, _ = collect(store)
    assert MESSAGE.encode() in context.model_json
    assert TaskResultRelease(store.fresh).check(DRAFT, context) == (
        ReleaseStatus.RELEASED, ReleasedResult(DRAFT.text, (TASK,)),
    )
    assert store.fresh_calls == ["open", "card", "comments", "close"]


def test_revoke_only_uncited_comment_after_read_blocks_all_free_text():
    store = Store()
    context, stale_tools = collect(store)
    gate = TaskResultRelease(store.fresh)
    assert gate.check(DRAFT, context)[0] is ReleaseStatus.RELEASED
    store.fresh_calls.clear()
    store.comment_visible = False
    assert stale_tools.read_task_comments(DAY, DAY)[1].fragments[0].source == MESSAGE

    assert gate.check(DRAFT, context) == (ReleaseStatus.UNAVAILABLE, None)
    assert store.fresh_calls == ["open", "card", "comments", "close"]


def test_dialogue_evidence_blocks_reuse_and_release_after_revocation():
    """Revalidate inherited uncited data before model reuse and before release."""
    store: Store = Store()
    previous, _ = collect(store)
    current, _ = collect(store, plan=())
    evidence = decode_evidence(encode_evidence(collect_evidence(previous)))
    gate: TaskResultRelease = TaskResultRelease(store.fresh)
    assert gate.check_history(evidence)
    store.comment_visible = False
    assert not gate.check_history(evidence)
    assert gate.check(DRAFT, current, inherited=evidence) == (ReleaseStatus.UNAVAILABLE, None)


@pytest.mark.parametrize("attribute", ["project_visible", "card_fields_visible"])
def test_revoke_initial_scope_or_field_blocks_a_result_citing_only_comment(attribute):
    store = Store()
    context, _ = collect(store)
    setattr(store, attribute, False)

    assert TaskResultRelease(store.fresh).check(ModelDraft("Text", (MESSAGE,)), context) == (
        ReleaseStatus.UNAVAILABLE, None,
    )
    assert store.fresh_calls == ["open", "card", "close"]


@pytest.mark.parametrize("draft", [
    {"text": "Text", "citations": []},
    ModelDraft(" ", ()),
    ModelDraft("\ud800", ()),
    ModelDraft("Text", [TASK]),
    ModelDraft("Text", ("/odoo/project.task/10",)),
    ModelDraft("Text", ("https://example.com/odoo/project.task/9",)),
])
def test_invalid_structure_or_unknown_citation_does_not_open_transaction(draft):
    store = Store()
    context, _ = collect(store)
    assert TaskResultRelease(store.fresh).check(draft, context) == (ReleaseStatus.INVALID_RESULT, None)
    assert not store.fresh_calls


def test_build_history_and_worklog_links_on_server():
    store = Store()
    context, _ = collect(store, (HISTORY, WORKLOG))
    draft = ModelDraft("Text", ("mail.message:17", LINE))
    assert TaskResultRelease(store.fresh).check(draft, context) == (
        ReleaseStatus.RELEASED, ReleasedResult("Text", (MESSAGE, LINE)),
    )
    assert store.fresh_calls == ["open", "card", "history", "worklog", "close"]


def test_even_no_citations_rechecks_and_late_close_blocks_result():
    store = Store(close_duration=2.0)
    context, _ = collect(store)
    gate = TaskResultRelease(store.fresh, clock=store.clock)
    assert gate.check(ModelDraft("Text", ()), context) == (ReleaseStatus.TIME_LIMIT, None)
    assert store.fresh_calls == ["open", "card", "comments", "close"]


def test_hidden_failure_cannot_turn_an_incomplete_recheck_into_a_release(caplog):
    class Broken:
        def card(self):
            raise RuntimeError("Sensitive data")

    @contextmanager
    def broken_transaction():
        with suppress(RuntimeError):
            yield TaskReadTools(Broken())

    context, _ = collect(Store())
    with caplog.at_level("INFO", logger=LOGGER_NAME):
        assert TaskResultRelease(broken_transaction).check(DRAFT, context) == (ReleaseStatus.FAILED, None)
    assert "task_result_release status=failed" in caplog.text
    assert all(value not in caplog.text for value in (DRAFT.text, TASK, MESSAGE, "Sensitive data"))
