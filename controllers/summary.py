"""Serve private operation status, risk summaries and their detail cards in Project."""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from importlib import import_module
from logging import Logger, getLogger
from os import environ
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo.exceptions import AccessError, MissingError
from odoo.http import Controller, Response, request, route
from odoo.tools import config

from ..adapters.odoo_background import queue_name
from ..adapters.odoo_risk_analysis import PrepareStatus, PreparedRiskAnalysis, prepare_risk_analysis
from ..adapters.openai_compatible import OpenAICompatibleModelAdapter
from ..adapters.settings import load_settings
from ..adapters.odoo_summary_access import can_read_summary
from ..adapters.odoo_summary_source import read_summary_tasks
from ..adapters.odoo_task_reader import readable_projects
from ..adapters.odoo_task_tools import OdooTaskSource
from ..adapters.rabbitmq import RabbitQueue
from ..adapters.sqlite_dialogue_store import DialogueScope, DialogueTurnKind, SQLiteDialogueStore
from ..adapters.sqlite_state_store import SQLiteStateStore
from ..application.operation_store import Operation, OperationState, StateConflict, StateStoreError, valid_company_ids
from ..application.project_time import DEFAULT_PROJECT_TIMEZONE, project_zone
from ..application.model_port import ModelFailure
from ..application.dialogue_provenance import Evidence, MAX_EVIDENCE, collect_evidence, decode_evidence, encode_evidence
from ..application.risk_analysis_graph import AnalysisStatus, RiskAnalysis, analyze_risk
from ..application.risk_analysis_model import RiskAnalysisModel
from ..application.risk_analysis_signal import RiskSignal
from ..application.task_read_plan import ReadBudget
from ..application.risk_summary import ProjectRiskSummary, SummaryRow, SummaryStatus, build_project_risk_summary
from ..application.summary_store import SavedSummary
from ..application.task_read_tools import ReadStatus, TaskCard

if TYPE_CHECKING:
    from odoo.api import Environment

_MENU: str = "/taskmaster/risk-summary"
_DETAIL: str = _MENU + "/<int:project_id>/tasks/<int:task_id>"
_MODULE: str = __name__.split(".")[-3]
_LOGGER: Logger = getLogger(__name__)
_HEADERS: tuple[tuple[str, str], ...] = (("Cache-Control", "private, no-store"), ("Vary", "Cookie"))
_MAX_ODOO_ID: int = 2**31 - 1
_PREVIEW_SIZE: int = 7
_SLOT_NAMESPACE: int = 0x544D5253
_MAX_SLOTS: int = 32
_RISK_LABELS: dict[str, str] = {
    "RR-01": "Просроченные задачи",
    "RR-02": "Близкий срок без движения",
    "RR-03": "Задачи без исполнителя",
    "RR-04": "Долго без активности",
}
_DEMO_ANSWER: str = (
    "Demo answer from a local model double. Check the dated facts above; "
    "ask the assignee to confirm the cause and the next step."
)


def _answer_text(text: str) -> str:
    """Translate only the known demo placeholder; e.g. _answer_text('Ответ') returns it unchanged."""
    return (
        "Демонстрационный ответ тестовой модели. Проверьте факты и даты выше; "
        "уточните у исполнителя причину и следующий шаг."
    ) if text.strip() == _DEMO_ANSWER else text


def _display_date(value: date | datetime) -> str:
    """Format a known date for display, converting instants to UTC; e.g. _display_date(date(2026, 11, 28))."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")
    return value.strftime("%d.%m.%Y")


def state_path(database: str) -> Path:
    """Return the per-database file in Odoo's private data directory.

    Example: state_path('test_db') gives the writer and reader the same path.
    """
    key: str = sha256(database.encode()).hexdigest()
    return Path(config["data_dir"]) / "taskmaster" / f"{key}.sqlite3"


def _open_store() -> SQLiteStateStore | None:
    """Read the active database's existing store without creating it.

    Example: _open_store() returns None on a fresh installation.
    """
    path: Path = state_path(request.env.cr.dbname)
    try:
        path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StateStoreError() from None
    return SQLiteStateStore(path, read_only=True)


def _company_scope() -> tuple[int, ...]:
    """Set the request's validated Odoo company scope and return its ordered IDs.

    Prefer the authenticated environment context, then the web client's cids
    cookie validated by Odoo; absent a selection, use the active company.
    Example: a valid cids cookie '3-1' yields (3, 1).
    """
    env = request.env
    ids = env.context.get("allowed_company_ids")
    if ids:
        if not isinstance(ids, (list, tuple)):
            raise AccessError("Invalid company selection.")
        companies: tuple[int, ...] = tuple(ids)
    elif cookie := request.cookies.get("cids"):
        parts: list[str] = cookie.split("-")
        if any(not part.isascii() or not part.isdecimal() or len(part) > 10 for part in parts):
            raise AccessError("Invalid company selection.")
        companies = tuple(int(part) for part in parts)
    else:
        companies = (env.company.id,)
    if not valid_company_ids(companies) or not set(companies).issubset(env.user._get_company_ids()):
        raise AccessError("Invalid company selection.")
    request.update_context(allowed_company_ids=list(companies))
    return companies


def _try_sync_slot(env: "Environment") -> bool:
    """Acquire a database-local transaction slot without waiting for another request.

    Example: _try_sync_slot(env) returns False while all configured slots are held.
    PostgreSQL releases a held slot at transaction end, including on process loss.
    """
    limit: int = int(environ.get("TASKMASTER_SYNC_SUMMARY_LIMIT", "1"))
    if not 1 <= limit <= _MAX_SLOTS:
        raise ValueError("The synchronous summary limit must be between 1 and 32.")
    for slot in range(limit):
        env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (_SLOT_NAMESPACE, slot))
        if env.cr.fetchone()[0] is True:
            return True
    return False


def _queue_summary(env: "Environment", project_id: int, companies: tuple[int, ...], zone: ZoneInfo) -> Operation:
    """Accept one durable background job using the server's queue configuration.

    Example: _queue_summary(env, 4, (3,), project_zone('UTC')) saves a manual operation.
    """
    if env.su or not env.user.active or not env.user.has_group("base.group_user"):
        raise AccessError("Background summaries require an internal user.")
    url: str = environ.get("TASKMASTER_AMQP_URL", "").strip()
    limit: int = int(environ.get("TASKMASTER_QUEUE_MAX_LENGTH", "0"))
    if not url or not callable(getattr(import_module("pika"), "Connection", None)):
        raise ValueError("The background queue is not configured.")
    RabbitQueue(url, queue_name(env.cr.dbname), limit)  # Validate configuration without connecting.
    path: Path = state_path(env.cr.dbname)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    return SQLiteStateStore(path).accept_background(
        str(uuid4()), owner_id=env.uid, project_id=project_id, recipient_id=env.uid,
        now=datetime.now(UTC), company_ids=companies, project_timezone=zone.key,
    )


class SummaryController(Controller):
    """Read and generate private project summaries for the current Odoo session."""

    @route("/taskmaster/api/projects/<int:project_id>/operations/<string:operation_id>", type="http", auth="user", methods=["GET"])
    def operation_status(self, project_id: int, operation_id: str, **_query: str) -> Response:
        """Read one owned operation and expose its result ID only after reauthorization.

        Example: GET /taskmaster/api/projects/4/operations/op-1 returns JSON.
        """
        not_found: dict[str, str] = {"error": "not_found"}
        try:
            _company_scope()
        except (AccessError, MissingError):
            return request.make_json_response(not_found, status=404, headers=_HEADERS)
        env = request.env
        if not 0 < project_id <= _MAX_ODOO_ID or len(operation_id) > 128 or not readable_projects(env, project_id):
            return request.make_json_response(not_found, status=404, headers=_HEADERS)
        try:
            store: SQLiteStateStore | None = _open_store()
            operation: Operation | None = (
                None if store is None else store.get_operation(operation_id, owner_id=env.uid, project_id=project_id)
            )
            if operation is None:
                return request.make_json_response(not_found, status=404, headers=_HEADERS)
            result_id: str | None = None
            if operation.state is OperationState.SUCCEEDED:
                if store is None or operation.result_id is None or operation.recipient_id != env.uid:
                    return request.make_json_response(not_found, status=404, headers=_HEADERS)
                saved: SavedSummary | None = store.get_summary(
                    operation.result_id, project_id=project_id, recipient_id=env.uid,
                    authorize=lambda snapshot: can_read_summary(env, snapshot),
                )
                if saved is None:
                    return request.make_json_response(not_found, status=404, headers=_HEADERS)
                result_id = saved.result_id
            return request.make_json_response({"status": operation.state.value, "result_id": result_id}, headers=_HEADERS)
        except StateStoreError:
            return request.make_json_response({"error": "storage_unavailable"}, status=503, headers=_HEADERS)

    @route(_MENU, type="http", auth="user", methods=["GET"])
    def risk_summary(self, project_id: str = "", expanded: str = "", result_id: str = "",
                     generation_failed: str = "", **_query: str) -> Response:
        """Render the latest authorized summary, showing up to seven rows by default.

        Pass the project, optional saved result ID, and expanded=1 to show all.
        Example: GET /taskmaster/risk-summary?project_id=4 opens the latest result.
        """
        try:
            _company_scope()
        except (AccessError, MissingError):
            return request.make_response("Сводка недоступна.", status=404, headers=_HEADERS)
        env = request.env
        projects = readable_projects(env)
        options: list[dict[str, object]] = []
        if projects:
            try:
                options = projects.read(["name"], load=None)
            except (AccessError, MissingError):
                pass
        selected: int | None = None
        if project_id:
            if len(project_id) <= 10 and project_id.isascii() and project_id.isdecimal():
                selected = int(project_id)
        elif len(options) == 1:
            selected = int(options[0]["id"])
        allowed: bool = any(item["id"] == selected for item in options)
        result: SavedSummary | None = None
        unavailable: bool = False
        if selected is not None and allowed and len(result_id) <= 128:
            try:
                store: SQLiteStateStore | None = _open_store()
                if store is not None:
                    def authorize(snapshot: SavedSummary) -> bool:
                        """Return the request's current decision; authorize(snapshot) never elevates rights."""
                        return can_read_summary(env, snapshot)

                    if result_id:
                        result = store.get_summary(result_id, project_id=selected, recipient_id=env.uid, authorize=authorize)
                    else:
                        result = store.get_latest_summary(project_id=selected, recipient_id=env.uid, authorize=authorize)
            except StateStoreError:
                unavailable = True
        rows: tuple[SummaryRow, ...] = () if result is None else result.summary.rows
        show_all: bool = expanded == "1"
        detail_query: str = "" if result is None else urlencode({
            "result_id": result.result_id, "expanded": "1" if show_all else "0",
        })
        return request.render(f"{_MODULE}.risk_summary", {
            "options": options, "selected": selected, "result": result,
            "visible_rows": rows if show_all else rows[:_PREVIEW_SIZE], "total_rows": len(rows),
            "can_toggle": len(rows) > _PREVIEW_SIZE, "show_all": show_all,
            "unavailable": unavailable, "requested": bool(project_id) or selected is not None,
            "selected_allowed": allowed, "generation_failed": generation_failed == "1" and allowed,
            "project_timezone": None if result is None else result.project_timezone,
            "risk_labels": _RISK_LABELS,
            "detail_query": detail_query,
            "stylesheet": f"/{_MODULE}/static/src/css/risk_summary.css?v=2", "display_date": _display_date,
            "waiting_script": f"/{_MODULE}/static/src/js/risk_summary.js?v=1",
        }, headers=_HEADERS, lazy=False)

    @route(_DETAIL, type="http", auth="user", methods=["GET"])
    def risk_detail(self, project_id: int, task_id: int, result_id: str = "",
                    expanded: str = "", dialogue_id: str = "", recover_dialogue: str = "", **_query: str) -> Response:
        """Show one saved risk row and a separately labeled current deadline.

        Require the exact result and the session recipient's current permissions.
        Example: GET /taskmaster/risk-summary/4/tasks/9?result_id=<id> opens
        the saved row for task 9, or returns a generic 404 if unavailable.
        """
        return self._show_risk_detail(project_id, task_id, result_id, expanded, dialogue_id=dialogue_id,
                                      recover_dialogue=recover_dialogue == "1")

    @route(_DETAIL + "/analyze", type="http", auth="user", methods=["POST"], csrf=True)
    def risk_analysis(self, project_id: int, task_id: int, result_id: str = "", question: str = "",
                      expanded: str = "", dialogue_id: str = "", **_form: str) -> Response:
        """Analyze one saved risk; an empty question requests an explanation of the signal."""
        if environ.get("TASKMASTER_ENABLE_RISK_ANALYSIS") != "1":
            return request.make_response("Разбор риска недоступен.", status=404, headers=_HEADERS)
        if type(question) is not str or len(question) > 2000 or "\x00" in question:
            return request.make_response("Недопустимый вопрос.", status=400, headers=_HEADERS)
        return self._show_risk_detail(project_id, task_id, result_id, expanded,
                                      question=question.strip(), dialogue_id=dialogue_id)

    def _show_risk_detail(self, project_id: int, task_id: int, result_id: str, expanded: str,
                          *, question: str | None = None, dialogue_id: str = "", recover_dialogue: bool = False) -> Response:
        """Render an authorized card, optionally with a freshly checked analysis."""
        def not_found() -> Response:
            """Return the same private 404 for missing and inaccessible cards."""
            return request.make_response("Карточка риска недоступна.", status=404, headers=_HEADERS)

        if not (0 < project_id <= _MAX_ODOO_ID and 0 < task_id <= _MAX_ODOO_ID and 0 < len(result_id) <= 128):
            return not_found()
        try:
            _company_scope()
            env: Environment = request.env
            store: SQLiteStateStore | None = _open_store()
            saved: SavedSummary | None = None if store is None else store.get_summary(
                result_id, project_id=project_id, recipient_id=env.uid,
                authorize=lambda snapshot: can_read_summary(env, snapshot),
            )
            if saved is None:
                return not_found()
            row: SummaryRow | None = next((item for item in saved.summary.rows if item.task_id == task_id), None)
            if row is None:
                return not_found()
            status: ReadStatus
            card: TaskCard | None
            status, card = OdooTaskSource(env, project_id=project_id, task_id=task_id, timezone=UTC).card()
            if status is not ReadStatus.AVAILABLE or card is None:
                return not_found()
        except (AccessError, MissingError):
            return not_found()
        except StateStoreError:
            _LOGGER.warning("Could not read or authorize the saved risk detail.")
            return not_found()

        summary_query: str = urlencode({
            "project_id": project_id, "result_id": saved.result_id, "expanded": "1" if expanded == "1" else "0",
        })
        historical_deadline: date | datetime | None = row.deadline
        if isinstance(historical_deadline, datetime):
            historical_deadline = historical_deadline.astimezone(UTC)
        current_deadline: date | datetime | None = card.deadline
        if isinstance(current_deadline, datetime):
            current_deadline = current_deadline.astimezone(UTC)
        analysis: RiskAnalysis | None = None
        analysis_enabled: bool = environ.get("TASKMASTER_ENABLE_RISK_ANALYSIS") == "1"
        if not analysis_enabled and question is None:
            dialogue_id, recover_dialogue = "", False
        dialogue_url: str | None = None
        analysis_failed: bool = False
        visible_turns: list[tuple[str, str | None]] = []
        if (question is not None or dialogue_id or recover_dialogue) and store is not None:
            try:
                if not analysis_enabled:
                    return not_found()
                if question is not None and not _try_sync_slot(env):
                    return request.make_response("Разбор занят. Повторите позже.", status=503, headers=_HEADERS)
                budget: ReadBudget = ReadBudget()
                prepared: PreparedRiskAnalysis = prepare_risk_analysis(
                    env, store, project_id=project_id, task_id=task_id, result_id=result_id, budget=budget,
                )
                if prepared.status is PrepareStatus.UNAVAILABLE:
                    return not_found()
                if (prepared.status is PrepareStatus.READY and prepared.run is not None and prepared.gate is not None
                        and prepared.snapshot is not None and prepared.row is not None):
                    gate = prepared.gate
                    inherited: tuple[Evidence, ...] = ()
                    path: Path = state_path(env.cr.dbname).with_suffix(".dialogue.sqlite3")
                    policy: tuple[str | None, str | None] = (
                        environ.get("TASKMASTER_DIALOGUE_RETENTION_DAYS"), environ.get("TASKMASTER_DIALOGUE_WINDOW"),
                    )
                    dialogues: SQLiteDialogueStore | None = None
                    dialogue_expires_at: datetime | None = None
                    if policy != (None, None) or dialogue_id or recover_dialogue:
                        if not all(policy):
                            return not_found() if dialogue_id or recover_dialogue else request.make_response(
                                "Разбор риска недоступен.", status=503, headers=_HEADERS,
                            )
                        days, window = map(int, policy)
                        if not (1 <= days <= 365 and 2 <= window <= 12):
                            raise ValueError("Invalid dialogue policy")
                        ttl: timedelta = timedelta(days=days)
                        scope: DialogueScope = DialogueScope(env.uid, project_id, task_id, result_id, tuple(env.companies.ids))
                        now: datetime = datetime.now(UTC)
                        if question is None and not path.is_file():
                            return not_found()
                        dialogues = SQLiteDialogueStore(path, read_only=question is None)
                        if recover_dialogue and not dialogue_id:
                            latest = dialogues.latest_dialogue(scope, now=now)
                            if latest is None:
                                return not_found()
                            dialogue_id = latest.dialogue_id
                        if dialogue_id:
                            dialogue = dialogues.get_dialogue(dialogue_id, scope, now=now) if len(dialogue_id) == 36 else None
                            if dialogue is None:
                                return not_found()
                        else:
                            dialogue = dialogues.start_dialogue(scope, now=now, ttl=ttl)
                            dialogue_id = dialogue.dialogue_id
                        dialogue_expires_at = dialogue.expires_at
                        dialogue_url = f"{_MENU}/{project_id}/tasks/{task_id}?" + urlencode({
                            "dialogue_id": dialogue_id, "result_id": result_id, "expanded": expanded,
                        })
                        turns = dialogues.recent_turns(dialogue_id, scope, now=now, window=window)
                        answers = {turn.question_turn_id: turn for turn in turns if turn.kind is DialogueTurnKind.ANSWER}
                        history_deadline: float = monotonic() + 2.0
                        for turn in reversed(turns):
                            if turn.kind is not DialogueTurnKind.QUESTION:
                                continue
                            answer = answers.get(turn.turn_id)
                            if answer is None:
                                visible_turns.append((turn.body, None))
                                continue
                            try:
                                proof = decode_evidence(answer.provenance)
                            except (ValueError, RecursionError):
                                continue
                            combined: tuple[Evidence, ...] = tuple(dict.fromkeys((*inherited, *proof)))
                            if (len(combined) <= MAX_EVIDENCE - budget.max_calls - 1
                                    and monotonic() < history_deadline
                                    and (combined == inherited or gate.check_history(combined, deadline=history_deadline))):
                                inherited = combined
                                visible_turns.append((turn.body, answer.body))
                        visible_turns.reverse()
                    if question is not None:
                        accepted = None if dialogues is None else dialogues.add_question(
                            dialogue_id, scope, question=question or "Объяснить сигнал", now=datetime.now(UTC), ttl=ttl,
                        )
                        try:
                            model: RiskAnalysisModel = RiskAnalysisModel(
                                OpenAICompatibleModelAdapter(load_settings()),
                                signal=RiskSignal.from_saved(prepared.snapshot, prepared.row),
                                history=tuple((q, a) for q, a in visible_turns if a is not None),
                                history_allowed=lambda: dialogue_expires_at is not None
                                and datetime.now(UTC) < dialogue_expires_at and gate.check_history(inherited),
                            )
                            analysis = analyze_risk(question, model=model, reading=prepared.run,
                                                    release=gate, inherited=inherited)
                            if analysis.status is AnalysisStatus.UNAVAILABLE:
                                return not_found()
                            if (dialogues is not None and accepted is not None and analysis.facts is not None
                                    and analysis.explanation is not None):
                                dialogues.record_answer(dialogue_id, accepted.turn_id, scope,
                                    answer=analysis.explanation.text,
                                    provenance=encode_evidence(collect_evidence(analysis.facts, inherited)),
                                    now=datetime.now(UTC), ttl=ttl)
                        except (ModelFailure, ValueError, StateStoreError, StateConflict):
                            analysis = None
                        analysis_failed = analysis is None or analysis.explanation is None
                        if analysis_failed:
                            visible_turns.clear()
                            if accepted is not None:
                                visible_turns.append((question, None))
                    if dialogue_expires_at is not None and datetime.now(UTC) >= dialogue_expires_at:
                        return not_found() if question is not None else self._show_risk_detail(
                            project_id, task_id, result_id, expanded,
                        )
                else:
                    analysis_failed = True
            except (ValueError, StateStoreError, StateConflict):
                analysis_failed = True
                visible_turns.clear()
            if analysis_failed and dialogue_url is None:
                return request.make_response("Не удалось выполнить разбор риска.", status=503, headers=_HEADERS)
        answer_text: str | None = None
        history_turns: list[tuple[str, str | None]] = list(visible_turns)
        if analysis is not None and analysis.explanation is not None:
            answer_text = _answer_text(analysis.explanation.text)
        elif question is None and visible_turns:
            latest_answer: str | None = visible_turns[-1][1]
            if latest_answer:
                answer_text = _answer_text(latest_answer)
                history_turns = history_turns[:-1]
            else:
                analysis_failed = True
        elif question is None and dialogue_id:
            analysis_failed = True
        # Preserve questions while hiding repeated boilerplate from the old demo model.
        history_turns = [(q, "" if a and a.strip() == _DEMO_ANSWER else a) for q, a in history_turns]
        recovery_url: str | None = None
        dialogue_path: Path = state_path(env.cr.dbname).with_suffix(".dialogue.sqlite3")
        if (analysis_enabled and dialogue_url is None and dialogue_path.is_file()
                and all(environ.get(key) for key in (
                    "TASKMASTER_DIALOGUE_RETENTION_DAYS", "TASKMASTER_DIALOGUE_WINDOW",
                ))):
            try:
                recovery_scope: DialogueScope = DialogueScope(
                    env.uid, project_id, task_id, result_id, tuple(env.companies.ids),
                )
                latest_dialogue = SQLiteDialogueStore(dialogue_path, read_only=True).latest_dialogue(
                    recovery_scope, now=datetime.now(UTC),
                )
                if latest_dialogue is not None:
                    recovery_url = f"{_MENU}/{project_id}/tasks/{task_id}?" + urlencode({
                        "result_id": result_id, "expanded": expanded, "dialogue_id": latest_dialogue.dialogue_id,
                    })
            except (ValueError, StateStoreError, StateConflict):
                recovery_url = None
        return request.render(f"{_MODULE}.risk_detail", {
            "row": row, "checked_at": saved.checked_at.astimezone(UTC),
            "historical_deadline": historical_deadline,
            "current_deadline": current_deadline, "current_deadline_known": card.deadline_known,
            "project_timezone": saved.project_timezone,
            "task_url": card.source, "summary_url": f"{_MENU}?{summary_query}",
            "risk_labels": _RISK_LABELS, "stylesheet": f"/{_MODULE}/static/src/css/risk_summary.css?v=2",
            "analysis_enabled": analysis_enabled,
            "analysis_url": f"{_MENU}/{project_id}/tasks/{task_id}/analyze",
            "analysis_result_id": result_id, "analysis_expanded": "1" if expanded == "1" else "0",
            "analysis": None if analysis is None else analysis.explanation,
            "analysis_failed": analysis_failed, "dialogue_id": dialogue_id, "dialogue_url": dialogue_url,
            "dialogue_turns": visible_turns,
            "history_turns": history_turns, "analysis_text": answer_text,
            "analysis_question": question if question is not None else (visible_turns[-1][0] if visible_turns else ""),
            "display_date": _display_date,
            "dialogue_recovery_url": recovery_url,
        }, headers=_HEADERS, lazy=False)

    @route(_MENU + "/generate", type="http", auth="user", methods=["POST"], csrf=True)
    def generate_summary(self, project_id: str = "", **_form: str) -> Response:
        """Generate a snapshot or accept a durable job for the session user.

        Example: POST project_id=4 with a valid CSRF token redirects with 303
        when a slot is open and returns 202 with an operation ID otherwise.
        """
        try:
            companies: tuple[int, ...] = _company_scope()
        except (AccessError, MissingError):
            return request.make_response("Сводка недоступна.", status=404, headers=_HEADERS)
        env = request.env
        selected: int = int(project_id) if len(project_id) <= 10 and project_id.isascii() and project_id.isdecimal() else 0
        project = readable_projects(env, selected) if 0 < selected <= _MAX_ODOO_ID else None
        if not project:
            return request.make_response("Сводка недоступна.", status=404, headers=_HEADERS)

        try:
            zone: ZoneInfo = project_zone(project.taskmaster_morning_timezone or DEFAULT_PROJECT_TIMEZONE)
            if not _try_sync_slot(env):
                operation: Operation = _queue_summary(env, selected, companies, zone)
                if operation.state is not OperationState.FAILED:
                    status_url: str = f"/taskmaster/api/projects/{selected}/operations/{operation.operation_id}"
                    return request.make_json_response(
                        {"operation_id": operation.operation_id}, status=202,
                        headers=(*_HEADERS, ("Location", status_url)),
                    )
                return request.make_json_response({"error": "generation_unavailable"}, status=503, headers=_HEADERS)
        except (AccessError, MissingError):
            return request.make_json_response({"error": "not_found"}, status=404, headers=_HEADERS)
        except (ImportError, ValueError, OSError, StateStoreError, StateConflict, ZoneInfoNotFoundError):
            return request.make_json_response({"error": "generation_unavailable"}, status=503, headers=_HEADERS)

        query: dict[str, int | str] = {"project_id": selected}
        try:
            checked_at: datetime = datetime.now(UTC)
            summary: ProjectRiskSummary = build_project_risk_summary(
                lambda: read_summary_tasks(env, selected),
                checked_at=checked_at, project_timezone=zone,
            )
            if summary.status is SummaryStatus.FAILED:
                query["generation_failed"] = "1"
            else:
                path: Path = state_path(env.cr.dbname)
                path.parent.mkdir(mode=0o700, exist_ok=True)
                saved: SavedSummary = SQLiteStateStore(path).save_summary(
                    str(uuid4()), project_id=selected, recipient_id=env.uid, checked_at=checked_at, summary=summary,
                    project_timezone=zone.key,
                )
                query["result_id"] = saved.result_id
        except (AccessError, MissingError, OSError, StateStoreError, StateConflict, ValueError, ZoneInfoNotFoundError):
            query["generation_failed"] = "1"
        response: Response = request.redirect(f"{_MENU}?{urlencode(query)}", code=303)
        response.headers.update(_HEADERS)
        return response
