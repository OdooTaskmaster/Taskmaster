"""Check isolated dialogue scope, immutable turns, bounded windows and expiry."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import inspect
from pathlib import Path
import sqlite3
from uuid import UUID

import pytest

from adapters.sqlite_dialogue_store import (
    MAX_PROVENANCE_BYTES, Dialogue, DialogueScope, DialogueTurn, DialogueTurnKind, SQLiteDialogueStore,
)
from application.operation_store import StateConflict, StateStoreError


_NOW: datetime = datetime(2026, 11, 27, 12, tzinfo=UTC)
_TTL: timedelta = timedelta(hours=2)
_SCOPE: DialogueScope = DialogueScope(7, 3, 11, "summary-1", (3, 1))


@pytest.fixture
def store(tmp_path: Path) -> SQLiteDialogueStore:
    """Create an isolated dialogue file; store(tmp_path) returns a SQLite test store."""
    return SQLiteDialogueStore(tmp_path / "dialogue.sqlite3")


def _start(store: SQLiteDialogueStore, scope: DialogueScope = _SCOPE,
           now: datetime = _NOW, ttl: timedelta = _TTL) -> Dialogue:
    """Start a known dialogue; _start(store) returns its server ID and scope."""
    return store.start_dialogue(scope, now=now, ttl=ttl)


def _ask(store: SQLiteDialogueStore, dialogue: Dialogue, question: str = "Why is this late?",
         now: datetime = _NOW, ttl: timedelta = _TTL) -> DialogueTurn:
    """Append one accepted question; _ask(store, dialogue) returns its turn."""
    return store.add_question(dialogue.dialogue_id, dialogue.scope, question=question, now=now, ttl=ttl)


def test_question_survives_alone_and_answer_is_chronological(store: SQLiteDialogueStore, tmp_path: Path) -> None:
    """Persist question before answer, reopen and keep order; returns None on success."""
    dialogue: Dialogue = _start(store)
    assert dialogue.scope == _SCOPE
    UUID(dialogue.dialogue_id)
    question: DialogueTurn = _ask(store, dialogue)
    UUID(question.turn_id)
    assert question.kind is DialogueTurnKind.QUESTION
    assert (question.body, question.question_turn_id, question.provenance) == ("Why is this late?", None, None)
    reopened: SQLiteDialogueStore = SQLiteDialogueStore(tmp_path / "dialogue.sqlite3")
    assert reopened.get_dialogue(dialogue.dialogue_id, _SCOPE, now=_NOW) == dialogue
    assert reopened.recent_turns(dialogue.dialogue_id, _SCOPE, now=_NOW, window=5) == (question,)
    answer: DialogueTurn = reopened.record_answer(
        dialogue.dialogue_id, question.turn_id, _SCOPE,
        answer="The stage changed after the deadline.", provenance='{"sources":["task:11"]}',
        now=_NOW + timedelta(seconds=1), ttl=_TTL,
    )
    UUID(answer.turn_id)
    assert answer.kind is DialogueTurnKind.ANSWER
    assert answer.question_turn_id == question.turn_id
    assert answer.provenance == '{"sources":["task:11"]}'
    turns: tuple[DialogueTurn, ...] = reopened.recent_turns(
        dialogue.dialogue_id, _SCOPE, now=_NOW + timedelta(seconds=2), window=5,
    )
    assert [turn.kind for turn in turns] == [DialogueTurnKind.QUESTION, DialogueTurnKind.ANSWER]
    assert [turn.body for turn in turns] == [question.body, answer.body]
    assert turns[1].created_at > turns[0].created_at


def test_answer_is_written_once_and_identical_retry_is_stable(store: SQLiteDialogueStore, tmp_path: Path) -> None:
    """Keep one answer per question and reject changed retries; returns None on success."""
    dialogue: Dialogue = _start(store)
    question: DialogueTurn = _ask(store, dialogue)
    answer: DialogueTurn = store.record_answer(
        dialogue.dialogue_id, question.turn_id, _SCOPE, answer="A", provenance="p1", now=_NOW, ttl=_TTL,
    )
    assert store.record_answer(
        dialogue.dialogue_id, question.turn_id, _SCOPE, answer="A", provenance="p1", now=_NOW, ttl=_TTL,
    ) == answer
    for body, provenance in (("B", "p1"), ("A", "p2")):
        with pytest.raises(StateConflict):
            store.record_answer(
                dialogue.dialogue_id, question.turn_id, _SCOPE,
                answer=body, provenance=provenance, now=_NOW, ttl=_TTL,
            )
    with sqlite3.connect(tmp_path / "dialogue.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM dialogue_turn WHERE question_turn_id = ?", (question.turn_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize("scope", [
    DialogueScope(8, 3, 11, "summary-1", (3, 1)),
    DialogueScope(7, 4, 11, "summary-1", (3, 1)),
    DialogueScope(7, 3, 12, "summary-1", (3, 1)),
    DialogueScope(7, 3, 11, "summary-2", (3, 1)),
    DialogueScope(7, 3, 11, "summary-1", (1, 3)),
])
def test_cross_scope_reads_and_writes_fail_closed(store: SQLiteDialogueStore, scope: DialogueScope) -> None:
    """Hide and reject another owner/project/task/result/company order; returns None."""
    dialogue: Dialogue = _start(store)
    question: DialogueTurn = _ask(store, dialogue)
    assert store.get_dialogue(dialogue.dialogue_id, scope, now=_NOW) is None
    assert store.recent_turns(dialogue.dialogue_id, scope, now=_NOW, window=5) == ()
    with pytest.raises(StateConflict):
        store.add_question(dialogue.dialogue_id, scope, question="Foreign?", now=_NOW, ttl=_TTL)
    with pytest.raises(StateConflict):
        store.record_answer(
            dialogue.dialogue_id, question.turn_id, scope,
            answer="Foreign", provenance="p", now=_NOW, ttl=_TTL,
        )
    assert [turn.body for turn in store.recent_turns(dialogue.dialogue_id, _SCOPE, now=_NOW, window=5)] == [
        question.body,
    ]


def test_separate_dialogues_in_one_scope_never_mix_turns(store: SQLiteDialogueStore) -> None:
    """Keep same-scope dialogues isolated by server dialogue ID; returns None on success."""
    first: Dialogue = _start(store)
    second: Dialogue = _start(store)
    assert first.dialogue_id != second.dialogue_id
    first_question: DialogueTurn = _ask(store, first, question="First dialogue")
    second_question: DialogueTurn = _ask(store, second, question="Second dialogue")
    with pytest.raises(StateConflict):
        store.record_answer(
            second.dialogue_id, first_question.turn_id, _SCOPE,
            answer="Wrong dialogue", provenance="p", now=_NOW, ttl=_TTL,
        )
    assert store.recent_turns(first.dialogue_id, _SCOPE, now=_NOW, window=5) == (first_question,)
    assert store.recent_turns(second.dialogue_id, _SCOPE, now=_NOW, window=5) == (second_question,)


def test_expiry_hides_reads_and_writes_purge_expired_rows(store: SQLiteDialogueStore, tmp_path: Path) -> None:
    """Never return expired turns, even read-only, and purge them on a later write; returns None."""
    dialogue: Dialogue = _start(store)
    _ask(store, dialogue)
    path: Path = tmp_path / "dialogue.sqlite3"
    before: datetime = _NOW + _TTL - timedelta(microseconds=1)
    assert store.get_dialogue(dialogue.dialogue_id, _SCOPE, now=before) is not None
    assert store.get_dialogue(dialogue.dialogue_id, _SCOPE, now=_NOW + _TTL) is None
    read_only: SQLiteDialogueStore = SQLiteDialogueStore(path, read_only=True)
    assert read_only.recent_turns(dialogue.dialogue_id, _SCOPE, now=_NOW + _TTL, window=5) == ()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM dialogue").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM dialogue_turn").fetchone() == (1,)
    fresh: Dialogue = _start(store, now=_NOW + _TTL)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dialogue WHERE dialogue_id = ?", (dialogue.dialogue_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM dialogue WHERE dialogue_id = ?", (fresh.dialogue_id,),
        ).fetchone() == (1,)


def test_recent_window_returns_only_last_turns_in_original_order(store: SQLiteDialogueStore) -> None:
    """Bound the window explicitly and keep chronological order; returns None on success."""
    dialogue: Dialogue = _start(store)
    turns: list[DialogueTurn] = [
        _ask(store, dialogue, question=f"Question {index}", now=_NOW + timedelta(seconds=index))
        for index in range(4)
    ]
    recent: tuple[DialogueTurn, ...] = store.recent_turns(
        dialogue.dialogue_id, _SCOPE, now=_NOW + timedelta(seconds=5), window=2,
    )
    assert [turn.turn_id for turn in recent] == [turns[2].turn_id, turns[3].turn_id]
    assert all(turn.expires_at >= turns[3].created_at for turn in recent)
    wider: tuple[DialogueTurn, ...] = store.recent_turns(
        dialogue.dialogue_id, _SCOPE, now=_NOW + timedelta(seconds=5), window=3,
    )
    assert [turn.turn_id for turn in wider] == [turns[1].turn_id, turns[2].turn_id, turns[3].turn_id]


def test_policy_clock_provenance_and_window_have_no_defaults(store: SQLiteDialogueStore) -> None:
    """Require explicit policy and cap opaque provenance bytes; returns None on success."""
    dialogue: Dialogue = _start(store)
    question: DialogueTurn = _ask(store, dialogue)
    for ttl in (timedelta(0), timedelta(seconds=-1), None, 1):
        with pytest.raises(ValueError):
            store.add_question(dialogue.dialogue_id, _SCOPE, question="Q", now=_NOW, ttl=ttl)  # type: ignore[arg-type]
    for window in (0, -1, True, "1"):
        with pytest.raises(ValueError):
            store.recent_turns(dialogue.dialogue_id, _SCOPE, now=_NOW, window=window)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.recent_turns(dialogue.dialogue_id, _SCOPE, now=datetime(2026, 11, 27, 12), window=1)
    assert MAX_PROVENANCE_BYTES == 32 * 1024
    exact: str = "x" * MAX_PROVENANCE_BYTES
    assert store.record_answer(
        dialogue.dialogue_id, question.turn_id, _SCOPE,
        answer="A", provenance=exact, now=_NOW, ttl=_TTL,
    ).provenance == exact
    for method, names in (
        (SQLiteDialogueStore.start_dialogue, ("now", "ttl")),
        (SQLiteDialogueStore.add_question, ("now", "ttl")),
        (SQLiteDialogueStore.record_answer, ("now", "ttl")),
        (SQLiteDialogueStore.recent_turns, ("now", "window")),
    ):
        parameters = inspect.signature(method).parameters
        assert all(parameters[name].default is inspect.Parameter.empty for name in names)


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"owner_id": 0}, "owner"),
    ({"project_id": True}, "project"),
    ({"task_id": -1}, "task"),
    ({"result_id": "  "}, "result"),
    ({"company_ids": ()}, "empty companies"),
    ({"company_ids": (3, 3)}, "duplicate companies"),
    ({"company_ids": [3, 1]}, "unordered type"),
])
def test_scope_rejects_invalid_identity(overrides: dict[str, object], reason: str) -> None:
    """Reject malformed owner/project/task/result/company scope; returns None on success."""
    values: dict[str, object] = {
        "owner_id": 7, "project_id": 3, "task_id": 11, "result_id": "summary-1", "company_ids": (3, 1),
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        DialogueScope(**values)  # type: ignore[arg-type]


def test_read_only_store_never_creates_or_sanitizes_failures(tmp_path: Path) -> None:
    """Leave a missing file absent and hide raw corruption details; returns None on success."""
    missing: Path = tmp_path / "missing.sqlite3"
    read_only: SQLiteDialogueStore = SQLiteDialogueStore(missing, read_only=True)
    assert not missing.exists()
    with pytest.raises(StateStoreError) as raised:
        read_only.get_dialogue("dialogue-1", _SCOPE, now=_NOW)
    assert "missing" not in str(raised.value)
    assert not missing.exists()


def test_corrupt_stored_metadata_is_sanitized(store: SQLiteDialogueStore, tmp_path: Path) -> None:
    """Fail with a fixed storage error instead of exposing stored text; returns None on success."""
    dialogue: Dialogue = _start(store)
    with sqlite3.connect(tmp_path / "dialogue.sqlite3") as connection:
        connection.execute("UPDATE dialogue SET created_at = ?", ("private raw text",))
    with pytest.raises(StateStoreError) as raised:
        store.get_dialogue(dialogue.dialogue_id, _SCOPE, now=_NOW)
    assert "private" not in str(raised.value)


def test_concurrent_conflicting_answers_have_one_winner(store: SQLiteDialogueStore, tmp_path: Path) -> None:
    """Race two answers for one question and keep one row; returns None on success."""
    dialogue: Dialogue = _start(store)
    question: DialogueTurn = _ask(store, dialogue)

    def answer(body: str) -> DialogueTurn | None:
        """Return the winning turn or None on conflict; answer(body) attempts one answer."""
        try:
            return store.record_answer(
                dialogue.dialogue_id, question.turn_id, _SCOPE,
                answer=body, provenance="p", now=_NOW, ttl=_TTL,
            )
        except StateConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results: list[DialogueTurn | None] = list(pool.map(answer, ("A", "B")))
    assert sum(result is not None for result in results) == 1
    with sqlite3.connect(tmp_path / "dialogue.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM dialogue_turn WHERE question_turn_id = ?", (question.turn_id,),
        ).fetchone() == (1,)