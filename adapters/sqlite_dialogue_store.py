"""Persist bounded risk-analysis dialogues in a dedicated local SQLite file."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

if __package__ and "." in __package__:
    from ..application.operation_store import StateConflict, StateStoreError, valid_company_ids
else:
    from application.operation_store import StateConflict, StateStoreError, valid_company_ids


MAX_PROVENANCE_BYTES: int = 32 * 1024

_SCOPE_FILTER: str = (
    "dialogue_id = ? AND owner_id = ? AND project_id = ? AND task_id = ? "
    "AND result_id = ? AND company_ids = ? AND expires_at > ?"
)


class DialogueTurnKind(StrEnum):
    """Name a persisted dialogue turn; DialogueTurnKind('answer') returns ANSWER."""

    QUESTION = "question"
    ANSWER = "answer"


@dataclass(frozen=True, slots=True)
class DialogueScope:
    """Carry the exact owner/project/task/result/company order of one dialogue.

    All IDs are positive, result_id is a nonblank stored key, and company_ids is
    a nonempty ordered tuple of distinct Odoo company IDs. Example:
    DialogueScope(7, 3, 11, 'summary-1', (3, 1)) scopes a manager's dialogue.
    """

    owner_id: int
    project_id: int
    task_id: int
    result_id: str
    company_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        """Reject malformed scope before it reaches SQLite; invalid scope raises ValueError."""
        if (any(type(value) is not int or value <= 0 for value in (self.owner_id, self.project_id, self.task_id))
                or not isinstance(self.result_id, str) or not self.result_id.strip()
                or not valid_company_ids(self.company_ids)):
            raise ValueError("A positive owner/project/task, result key and ordered companies are required.")


@dataclass(frozen=True, slots=True)
class Dialogue:
    """Carry the server dialogue ID, immutable scope and retained time window.

    Example: store.start_dialogue(scope, now=now, ttl=ttl) returns a Dialogue
    whose expires_at is now + ttl.
    """

    dialogue_id: str
    scope: DialogueScope
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    """Carry one immutable question or verified answer in chronological dialogue order.

    Answers carry the answered question_turn_id and an opaque provenance string
    supplied by the application. Example: an answer turn returned by
    record_answer(..., provenance='{"sources":[]}', ...) keeps that exact value.
    """

    turn_id: str
    dialogue_id: str
    kind: DialogueTurnKind
    body: str
    created_at: datetime
    expires_at: datetime
    question_turn_id: str | None = None
    provenance: str | None = None


class SQLiteDialogueStore:
    """Store bounded dialogues in a local SQLite file with short-lived transactions.

    Supply a local file path whose parent already exists with restricted access.
    The server assigns a different file to each Odoo database. Construction
    initializes dialogue tables but never creates Odoo data. Reads always exclude
    expired rows, including in read_only mode; writes delete expired dialogues.
    Example: SQLiteDialogueStore(path) keeps no live connection between calls.
    """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        """Open the store without creating a missing read-only file.

        Example: SQLiteDialogueStore(path, read_only=True) only reads existing rows.
        """
        self._path: Path = Path(path)
        self._read_only: bool = read_only
        if read_only:
            return
        with self._connection() as connection:
            mode: str = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            if mode != "delete":
                raise StateStoreError()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS dialogue (
                    dialogue_id TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    task_id INTEGER NOT NULL,
                    result_id TEXT NOT NULL,
                    company_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS dialogue_turn (
                    turn_no INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL UNIQUE,
                    dialogue_id TEXT NOT NULL REFERENCES dialogue(dialogue_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('question', 'answer')),
                    body TEXT NOT NULL,
                    question_turn_id TEXT REFERENCES dialogue_turn(turn_id) ON DELETE CASCADE,
                    provenance TEXT,
                    created_at TEXT NOT NULL,
                    CHECK ((kind = 'question' AND question_turn_id IS NULL AND provenance IS NULL)
                        OR (kind = 'answer' AND question_turn_id IS NOT NULL AND provenance IS NOT NULL))
                )
            """)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS dialogue_answer_once "
                "ON dialogue_turn (question_turn_id) WHERE question_turn_id IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS dialogue_turn_order ON dialogue_turn (dialogue_id, turn_no)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS dialogue_scope "
                "ON dialogue (owner_id, project_id, task_id, result_id)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection and sanitize storage failures.

        Example: with store._connection() as connection: ... commits on success.
        """
        try:
            location: Path | str = self._path.absolute().as_uri() + "?mode=ro" if self._read_only else self._path
            with closing(sqlite3.connect(location, uri=self._read_only, timeout=3, isolation_level="IMMEDIATE")) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                if self._read_only:
                    if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                        raise StateStoreError()
                else:
                    connection.execute("PRAGMA synchronous=EXTRA")
                with connection:
                    yield connection
        except (sqlite3.Error, OSError):
            raise StateStoreError() from None

    @staticmethod
    def _time_text(value: datetime) -> str:
        """Render an aware time as fixed-width UTC text; _time_text(now) returns ISO text."""
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    @classmethod
    def _aware_time(cls, value: datetime, label: str) -> datetime:
        """Require an aware clock and return it in UTC; an invalid clock raises ValueError."""
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError(f"The {label} must be timezone-aware.")
        return value.astimezone(UTC)

    @staticmethod
    def _expiry(now: datetime, ttl: timedelta) -> datetime:
        """Require an explicit positive retention window; an invalid TTL raises ValueError."""
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("The dialogue TTL must be a positive explicit timedelta.")
        return now + ttl

    @staticmethod
    def _identifier(value: str, label: str) -> str:
        """Require a nonblank ID; an invalid identifier raises ValueError."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"The {label} must be a nonblank string.")
        return value

    @staticmethod
    def _text(value: str, label: str) -> str:
        """Require nonblank dialogue text without rewriting it; blank text raises ValueError."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"The dialogue {label} must be nonblank text.")
        return value

    @staticmethod
    def _provenance(value: str) -> str:
        """Require the application's opaque provenance within the fixed byte cap.

        An invalid type or a value above MAX_PROVENANCE_BYTES raises ValueError.
        Example: _provenance('{"sources":[]}') returns the same string.
        """
        if not isinstance(value, str):
            raise ValueError("The answer provenance must be an opaque string.")
        if len(value.encode("utf-8")) > MAX_PROVENANCE_BYTES:
            raise ValueError("The answer provenance exceeds the supported size.")
        return value

    @staticmethod
    def _require_scope(scope: DialogueScope) -> DialogueScope:
        """Require the store's exact scope type; a foreign value raises ValueError."""
        if type(scope) is not DialogueScope:
            raise ValueError("A DialogueScope is required.")
        return scope

    @staticmethod
    def _scope_values(dialogue_id: str, scope: DialogueScope) -> tuple[object, ...]:
        """Build bound identity parameters in the same order as _SCOPE_FILTER without expiry."""
        return (
            dialogue_id, scope.owner_id, scope.project_id, scope.task_id, scope.result_id,
            json.dumps(list(scope.company_ids)),
        )

    @staticmethod
    def _purge(connection: sqlite3.Connection, now_text: str) -> None:
        """Delete expired dialogues and their turns as part of a write transaction."""
        connection.execute("DELETE FROM dialogue WHERE expires_at <= ?", (now_text,))

    @staticmethod
    def _dialogue(row: sqlite3.Row) -> Dialogue:
        """Decode a stored dialogue or fail safely on corrupt metadata."""
        try:
            decoded: object = json.loads(row["company_ids"])
            if not isinstance(decoded, list):
                raise ValueError
            scope: DialogueScope = DialogueScope(
                row["owner_id"], row["project_id"], row["task_id"], row["result_id"], tuple(decoded),
            )
            created_at: datetime = datetime.fromisoformat(row["created_at"])
            expires_at: datetime = datetime.fromisoformat(row["expires_at"])
            if (created_at.utcoffset() is None or expires_at.utcoffset() is None
                    or expires_at < created_at
                    or not isinstance(row["dialogue_id"], str) or not row["dialogue_id"].strip()):
                raise ValueError
            return Dialogue(row["dialogue_id"], scope, created_at, expires_at)
        except (ValueError, TypeError):
            raise StateStoreError() from None

    @staticmethod
    def _turn(row: sqlite3.Row, expires_at: datetime) -> DialogueTurn:
        """Decode one stored turn with its current dialogue expiry; corruption fails safely."""
        try:
            kind: DialogueTurnKind = DialogueTurnKind(row["kind"])
            created_at: datetime = datetime.fromisoformat(row["created_at"])
            question_turn_id: object = row["question_turn_id"]
            provenance: object = row["provenance"]
            if (not isinstance(row["turn_id"], str) or not row["turn_id"].strip()
                    or not isinstance(row["dialogue_id"], str) or not row["dialogue_id"].strip()
                    or not isinstance(row["body"], str) or not row["body"].strip()
                    or created_at.utcoffset() is None or expires_at.utcoffset() is None
                    or expires_at < created_at):
                raise ValueError
            if kind is DialogueTurnKind.QUESTION:
                if question_turn_id is not None or provenance is not None:
                    raise ValueError
            elif (not isinstance(question_turn_id, str) or not question_turn_id.strip()
                    or not isinstance(provenance, str)
                    or len(provenance.encode("utf-8")) > MAX_PROVENANCE_BYTES):
                raise ValueError
            return DialogueTurn(
                row["turn_id"], row["dialogue_id"], kind, row["body"], created_at, expires_at,
                question_turn_id, provenance,
            )
        except (ValueError, TypeError):
            raise StateStoreError() from None

    def start_dialogue(self, scope: DialogueScope, *, now: datetime, ttl: timedelta) -> Dialogue:
        """Create a server-identified dialogue and purge expired rows first.

        Pass the exact scope, an aware clock and a positive explicit TTL. The
        returned ID is a fresh server UUID; no caller-supplied ID is accepted.
        Example: store.start_dialogue(scope, now=now, ttl=timedelta(hours=1))
        returns a retained Dialogue.
        """
        checked_scope: DialogueScope = self._require_scope(scope)
        checked_now: datetime = self._aware_time(now, "clock")
        expires_at: datetime = self._expiry(checked_now, ttl)
        now_text: str = self._time_text(checked_now)
        expires_text: str = self._time_text(expires_at)
        dialogue_id: str = str(uuid4())
        with self._connection() as connection:
            self._purge(connection, now_text)
            connection.execute(
                """INSERT INTO dialogue
                   (dialogue_id, owner_id, project_id, task_id, result_id, company_ids, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (*self._scope_values(dialogue_id, checked_scope), now_text, expires_text),
            )
            row: sqlite3.Row | None = connection.execute(
                f"SELECT * FROM dialogue WHERE {_SCOPE_FILTER}",
                (*self._scope_values(dialogue_id, checked_scope), now_text),
            ).fetchone()
            if row is None:
                raise StateStoreError()
            return self._dialogue(row)

    def get_dialogue(self, dialogue_id: str, scope: DialogueScope, *, now: datetime) -> Dialogue | None:
        """Return a dialogue only in the exact scope and current retention window.

        Example: store.get_dialogue('dialogue-1', scope, now=now) returns None
        for a missing, cross-scope or expired dialogue.
        """
        checked_id: str = self._identifier(dialogue_id, "dialogue ID")
        checked_scope: DialogueScope = self._require_scope(scope)
        now_text: str = self._time_text(self._aware_time(now, "clock"))
        with self._connection() as connection:
            row: sqlite3.Row | None = connection.execute(
                f"SELECT * FROM dialogue WHERE {_SCOPE_FILTER}",
                (*self._scope_values(checked_id, checked_scope), now_text),
            ).fetchone()
            return None if row is None else self._dialogue(row)

    def latest_dialogue(self, scope: DialogueScope, *, now: datetime) -> Dialogue | None:
        """Find the newest live dialogue in the exact scope; latest_dialogue(scope, now=now) may return None."""
        checked: DialogueScope = self._require_scope(scope)
        now_text: str = self._time_text(self._aware_time(now, "clock"))
        with self._connection() as connection:
            row: sqlite3.Row | None = connection.execute(
                """SELECT * FROM dialogue WHERE owner_id = ? AND project_id = ? AND task_id = ?
                   AND result_id = ? AND company_ids = ? AND expires_at > ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (*self._scope_values("", checked)[1:], now_text),
            ).fetchone()
            return None if row is None else self._dialogue(row)

    def add_question(self, dialogue_id: str, scope: DialogueScope, *, question: str,
                     now: datetime, ttl: timedelta) -> DialogueTurn:
        """Append an immutable accepted question before analysis starts.

        The dialogue must exist in the exact scope and remain unexpired. The
        write purges expired rows without extending this dialogue's retention. The
        returned turn ID is a fresh server UUID. Example:
        store.add_question(dialogue_id, scope, question='Why?', now=now, ttl=ttl)
        returns a question turn even when no answer exists yet.
        """
        checked_id: str = self._identifier(dialogue_id, "dialogue ID")
        checked_scope: DialogueScope = self._require_scope(scope)
        body: str = self._text(question, "question")
        checked_now: datetime = self._aware_time(now, "clock")
        self._expiry(checked_now, ttl)
        now_text: str = self._time_text(checked_now)
        turn_id: str = str(uuid4())
        with self._connection() as connection:
            self._purge(connection, now_text)
            dialogue_row: sqlite3.Row | None = connection.execute(
                f"SELECT * FROM dialogue WHERE {_SCOPE_FILTER}",
                (*self._scope_values(checked_id, checked_scope), now_text),
            ).fetchone()
            if dialogue_row is None:
                raise StateConflict()
            stored_expiry: datetime = self._dialogue(dialogue_row).expires_at
            connection.execute(
                """INSERT INTO dialogue_turn
                   (turn_id, dialogue_id, kind, body, question_turn_id, provenance, created_at)
                   VALUES (?, ?, 'question', ?, NULL, NULL, ?)""",
                (turn_id, checked_id, body, now_text),
            )
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM dialogue_turn WHERE turn_id = ? AND dialogue_id = ?", (turn_id, checked_id),
            ).fetchone()
            if row is None:
                raise StateStoreError()
            return self._turn(row, stored_expiry)

    def record_answer(self, dialogue_id: str, question_turn_id: str, scope: DialogueScope, *,
                      answer: str, provenance: str, now: datetime, ttl: timedelta) -> DialogueTurn:
        """Write the optional verified answer to one question at most once.

        Provenance is the application's opaque serialized string; this adapter
        does not parse Odoo projections, resolve sources or authorize reads.
        The dialogue and question must exist in the exact unexpired scope.
        An identical retry returns the stored answer; a different retry raises
        StateConflict. Example: store.record_answer(dialogue_id, question_id,
        scope, answer='Because...', provenance='{"sources":[]}', now=now,
        ttl=ttl) returns the answer turn.
        """
        checked_id: str = self._identifier(dialogue_id, "dialogue ID")
        checked_question: str = self._identifier(question_turn_id, "question turn ID")
        checked_scope: DialogueScope = self._require_scope(scope)
        body: str = self._text(answer, "answer")
        stored_provenance: str = self._provenance(provenance)
        checked_now: datetime = self._aware_time(now, "clock")
        self._expiry(checked_now, ttl)
        now_text: str = self._time_text(checked_now)
        answer_id: str = str(uuid4())
        with self._connection() as connection:
            self._purge(connection, now_text)
            dialogue_row: sqlite3.Row | None = connection.execute(
                f"SELECT * FROM dialogue WHERE {_SCOPE_FILTER}",
                (*self._scope_values(checked_id, checked_scope), now_text),
            ).fetchone()
            if dialogue_row is None:
                raise StateConflict()
            dialogue: Dialogue = self._dialogue(dialogue_row)
            question_row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM dialogue_turn WHERE turn_id = ? AND dialogue_id = ? AND kind = 'question'",
                (checked_question, checked_id),
            ).fetchone()
            if question_row is None:
                raise StateConflict()
            inserted: sqlite3.Cursor = connection.execute(
                """INSERT INTO dialogue_turn
                   (turn_id, dialogue_id, kind, body, question_turn_id, provenance, created_at)
                   VALUES (?, ?, 'answer', ?, ?, ?, ?)
                   ON CONFLICT (question_turn_id) WHERE question_turn_id IS NOT NULL DO NOTHING""",
                (answer_id, checked_id, body, checked_question, stored_provenance, now_text),
            )
            if inserted.rowcount == 1:
                row: sqlite3.Row | None = connection.execute(
                    "SELECT * FROM dialogue_turn WHERE turn_id = ? AND dialogue_id = ?", (answer_id, checked_id),
                ).fetchone()
                if row is None:
                    raise StateStoreError()
                return self._turn(row, dialogue.expires_at)
            existing: sqlite3.Row | None = connection.execute(
                "SELECT * FROM dialogue_turn WHERE question_turn_id = ? AND dialogue_id = ? AND kind = 'answer'",
                (checked_question, checked_id),
            ).fetchone()
            if existing is None:
                raise StateStoreError()
            stored: DialogueTurn = self._turn(existing, dialogue.expires_at)
            if (stored.body, stored.provenance) != (body, stored_provenance):
                raise StateConflict()
            return stored

    def recent_turns(self, dialogue_id: str, scope: DialogueScope, *, now: datetime,
                     window: int) -> tuple[DialogueTurn, ...]:
        """Return the last explicitly bounded turns of one live dialogue in order.

        Pass an aware clock and the approved positive window; no window default
        is assumed, and expired turns are never returned, including read-only.
        Example: store.recent_turns(dialogue_id, scope, now=now, window=4)
        returns up to four turns oldest to newest.
        """
        checked_id: str = self._identifier(dialogue_id, "dialogue ID")
        checked_scope: DialogueScope = self._require_scope(scope)
        now_text: str = self._time_text(self._aware_time(now, "clock"))
        if type(window) is not int or window <= 0:
            raise ValueError("The dialogue window must be a positive explicit integer.")
        with self._connection() as connection:
            rows: list[sqlite3.Row] = connection.execute(
                f"""SELECT turn.*, dialogue.expires_at AS dialogue_expires_at
                    FROM dialogue_turn AS turn
                    JOIN dialogue ON dialogue.dialogue_id = turn.dialogue_id
                    WHERE dialogue.dialogue_id = ? AND dialogue.owner_id = ? AND dialogue.project_id = ?
                      AND dialogue.task_id = ? AND dialogue.result_id = ? AND dialogue.company_ids = ?
                      AND dialogue.expires_at > ?
                    ORDER BY turn.turn_no DESC LIMIT ?""",
                (*self._scope_values(checked_id, checked_scope), now_text, window),
            ).fetchall()
            if not rows:
                return ()
            try:
                expires_at: datetime = datetime.fromisoformat(rows[0]["dialogue_expires_at"])
            except (ValueError, TypeError):
                raise StateStoreError() from None
            return tuple(self._turn(row, expires_at) for row in reversed(rows))
