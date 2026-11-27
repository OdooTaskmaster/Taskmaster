"""Serve private operation status and a server-rendered risk summary in Project."""

from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from os import environ
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo.exceptions import AccessError, MissingError
from odoo.http import Controller, Response, request, route
from odoo.tools import config

from ..adapters.odoo_background import queue_name
from ..adapters.odoo_summary_access import can_read_summary
from ..adapters.odoo_summary_source import read_summary_tasks
from ..adapters.odoo_task_reader import readable_projects
from ..adapters.rabbitmq import RabbitQueue
from ..adapters.sqlite_state_store import SQLiteStateStore
from ..application.operation_store import Operation, OperationState, StateConflict, StateStoreError, valid_company_ids
from ..application.risk_summary import ProjectRiskSummary, SummaryRow, SummaryStatus, build_project_risk_summary
from ..application.summary_store import SavedSummary

if TYPE_CHECKING:
    from odoo.api import Environment

_MENU: str = "/taskmaster/risk-summary"
_MODULE: str = __name__.split(".")[-3]
_HEADERS: tuple[tuple[str, str], ...] = (("Cache-Control", "private, no-store"), ("Vary", "Cookie"))
_PROJECT_MAX_ID: int = 2**31 - 1
_PILOT_TIMEZONE: str = "Europe/Moscow"
_PREVIEW_SIZE: int = 7
_SLOT_NAMESPACE: int = 0x544D5253
_MAX_SLOTS: int = 32
_RISK_LABELS: dict[str, str] = {
    "RR-01": "Просроченные задачи",
    "RR-02": "Близкий срок без движения",
    "RR-03": "Задачи без исполнителя",
    "RR-04": "Долго без активности",
}


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


def _queue_summary(env: "Environment", project_id: int, companies: tuple[int, ...]) -> Operation:
    """Accept one durable background job using the server's queue configuration.

    Example: _queue_summary(env, 4, (3,)) returns the saved operation for project 4.
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
        now=datetime.now(UTC), company_ids=companies,
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
        if not 0 < project_id <= _PROJECT_MAX_ID or len(operation_id) > 128 or not readable_projects(env, project_id):
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
        return request.render(f"{_MODULE}.risk_summary", {
            "options": options, "selected": selected, "result": result,
            "visible_rows": rows if show_all else rows[:_PREVIEW_SIZE], "total_rows": len(rows),
            "can_toggle": len(rows) > _PREVIEW_SIZE, "show_all": show_all,
            "unavailable": unavailable, "requested": bool(project_id) or selected is not None,
            "selected_allowed": allowed, "generation_failed": generation_failed == "1" and allowed,
            "project_timezone": _PILOT_TIMEZONE,
            "risk_labels": _RISK_LABELS,
            "stylesheet": f"/{_MODULE}/static/src/css/risk_summary.css?v=1",
            "waiting_script": f"/{_MODULE}/static/src/js/risk_summary.js?v=1",
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
        if not 0 < selected <= _PROJECT_MAX_ID or not readable_projects(env, selected):
            return request.make_response("Сводка недоступна.", status=404, headers=_HEADERS)

        try:
            if not _try_sync_slot(env):
                operation: Operation = _queue_summary(env, selected, companies)
                if operation.state is not OperationState.FAILED:
                    status_url: str = f"/taskmaster/api/projects/{selected}/operations/{operation.operation_id}"
                    return request.make_json_response(
                        {"operation_id": operation.operation_id}, status=202,
                        headers=(*_HEADERS, ("Location", status_url)),
                    )
                return request.make_json_response({"error": "generation_unavailable"}, status=503, headers=_HEADERS)
        except (AccessError, MissingError):
            return request.make_json_response({"error": "not_found"}, status=404, headers=_HEADERS)
        except (ImportError, ValueError, OSError, StateStoreError, StateConflict):
            return request.make_json_response({"error": "generation_unavailable"}, status=503, headers=_HEADERS)

        query: dict[str, int | str] = {"project_id": selected}
        try:
            checked_at: datetime = datetime.now(UTC)
            summary: ProjectRiskSummary = build_project_risk_summary(
                lambda: read_summary_tasks(env, selected),
                checked_at=checked_at, project_timezone=ZoneInfo(_PILOT_TIMEZONE),
            )
            if summary.status is SummaryStatus.FAILED:
                query["generation_failed"] = "1"
            else:
                path: Path = state_path(env.cr.dbname)
                path.parent.mkdir(mode=0o700, exist_ok=True)
                saved: SavedSummary = SQLiteStateStore(path).save_summary(
                    str(uuid4()), project_id=selected, recipient_id=env.uid, checked_at=checked_at, summary=summary,
                )
                query["result_id"] = saved.result_id
        except (AccessError, MissingError, OSError, StateStoreError, StateConflict, ValueError, ZoneInfoNotFoundError):
            query["generation_failed"] = "1"
        response: Response = request.redirect(f"{_MENU}?{urlencode(query)}", code=303)
        response.headers.update(_HEADERS)
        return response
