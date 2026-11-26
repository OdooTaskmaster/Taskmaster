"""Fixture loading, mock tool execution, and source accounting for AG-01."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping


MAX_PERIOD_DAYS = 90
SERVER_COMMENT_CAP = 5
SERVER_DESCRIPTION_CHARS = 1200
SERVER_TOOL_CALLS = 12


@dataclass(frozen=True)
class Fixture:
    """Synthetic Odoo project used behind the five read tools."""

    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "Fixture":
        """Load a fixture from JSON."""

        with path.open("r", encoding="utf-8") as handle:
            data: dict[str, Any] = json.load(handle)
        return cls(data=data)

    @property
    def project(self) -> dict[str, Any]:
        """Return the project record."""

        return self.data["project"]

    def task(self, task_id: str) -> dict[str, Any] | None:
        """Return one task or None."""

        for task in self.data.get("tasks", []):
            if task.get("id") == task_id:
                return task
        return None

    def user(self, user_id: str) -> dict[str, Any] | None:
        """Return one user or None."""

        for user in self.data.get("users", []):
            if user.get("id") == user_id:
                return user
        return None

    def team_name(self, team_id: str) -> str:
        """Return a readable team name."""

        for team in self.data.get("teams", []):
            if team.get("id") == team_id:
                return str(team.get("name", team_id))
        return team_id

    def display_name(self, user_id: str | None) -> str | None:
        """Return a readable user name."""

        if not user_id:
            return None
        user = self.user(user_id)
        return str(user.get("name", user_id)) if user else user_id

    def can_access(self, user_id: str, task_id: str) -> bool:
        """Check project/team access without revealing a task to the caller."""

        task = self.task(task_id)
        user = self.user(user_id)
        if task is None or user is None:
            return False
        if user.get("role") == "manager":
            return True
        return task.get("team_id") in user.get("team_ids", [])


@dataclass
class ToolCallRecord:
    """One executed or rejected tool call."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass
class ToolServer:
    """Execute the five read tools with server-side scope and permissions."""

    fixture: Fixture
    task_id: str
    user_id: str
    tool_errors: Mapping[str, str] = field(default_factory=dict)
    calls: list[ToolCallRecord] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set, init=False)
    _call_count: int = field(default=0, init=False)
    _allowed: bool = field(init=False)
    _task: dict[str, Any] | None = field(init=False)

    def __post_init__(self) -> None:
        self._allowed = self.fixture.can_access(self.user_id, self.task_id)
        self._task = self.fixture.task(self.task_id)

    @property
    def allowed(self) -> bool:
        """Return whether the server confirmed access."""

        return self._allowed

    def execute(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and execute one tool call; never accept scope overrides."""

        args: dict[str, Any] = dict(arguments)
        self._call_count += 1
        if self._call_count > SERVER_TOOL_CALLS:
            return self._record(name, args, self._error("Лимит чтения исчерпан."))

        key: str = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        if key in self._seen:
            return self._record(
                name,
                args,
                self._error("Повторный запрос без новых оснований не выполняется."),
            )
        self._seen.add(key)

        if not self._allowed:
            return self._record(name, args, self._denied())
        if name in self.tool_errors:
            return self._record(name, args, self._error(self.tool_errors[name]))

        result: dict[str, Any]
        if name == "read_task_card":
            result = self._read_card(args)
        elif name == "read_task_description":
            result = self._read_description(args)
        elif name == "read_task_stage_history":
            result = self._read_stage_history(args)
        elif name == "read_task_comments":
            result = self._read_comments(args)
        elif name == "read_task_worklog":
            result = self._read_worklog(args)
        else:
            result = self._error("Инструмент недоступен.")
        return self._record(name, args, result)

    def _record(
        self, name: str, arguments: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(ToolCallRecord(name=name, arguments=arguments, result=result))
        return result

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"status": "error", "source_id": None, "message": message}

    @staticmethod
    def _denied() -> dict[str, Any]:
        return {
            "status": "denied",
            "source_id": None,
            "message": "Источник недоступен.",
        }

    @staticmethod
    def _empty(source_id: str, message: str) -> dict[str, Any]:
        return {"status": "empty", "source_id": source_id, "message": message}

    def _read_card(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return self._error("Инструмент не принимает параметры.")
        if self._task is None:
            return self._denied()
        return {
            "status": "ok",
            "source_id": f"task:{self.task_id}",
            "task": {
                "id": self._task["id"],
                "title": self._task["title"],
                "status": self._task["status"],
                "deadline": self._task.get("deadline"),
                "assignee": self.fixture.display_name(self._task.get("assignee_id")),
                "team": self.fixture.team_name(str(self._task.get("team_id", ""))),
            },
        }

    def _read_description(self, args: dict[str, Any]) -> dict[str, Any]:
        if args:
            return self._error("Инструмент не принимает параметры.")
        if self._task is None:
            return self._denied()
        text: str = str(self._task.get("description") or "")
        source_id: str = f"description:{self.task_id}"
        if not text:
            return self._empty(source_id, "Описание не заполнено.")
        returned: str = text[:SERVER_DESCRIPTION_CHARS]
        truncated: bool = len(text) > len(returned)
        result: dict[str, Any] = {
            "status": "ok",
            "source_id": source_id,
            "text": returned,
            "total_chars": len(text),
            "returned_chars": len(returned),
            "truncated": truncated,
        }
        if truncated:
            result["message"] = "Показан только первый фрагмент описания."
        return result

    def _parse_period(
        self, args: dict[str, Any], allowed: set[str]
    ) -> tuple[date, date, bool] | str:
        """Validate a period; clamp long ranges instead of rejecting them."""

        extra: set[str] = set(args) - allowed
        if extra:
            return f"Лишние параметры: {', '.join(sorted(extra))}."
        raw_from: Any = args.get("date_from")
        raw_to: Any = args.get("date_to")
        if not isinstance(raw_from, str) or not isinstance(raw_to, str):
            return "Нужны date_from и date_to в формате YYYY-MM-DD."
        try:
            start: date = date.fromisoformat(raw_from)
            end: date = date.fromisoformat(raw_to)
        except ValueError:
            return "Даты должны быть в формате YYYY-MM-DD."
        if start > end:
            return "date_from не может быть позже date_to."
        clamped: bool = False
        if (end - start).days > MAX_PERIOD_DAYS:
            start = end - timedelta(days=MAX_PERIOD_DAYS)
            clamped = True
        return start, end, clamped

    @staticmethod
    def _clamp_note(clamped: bool) -> dict[str, Any]:
        if not clamped:
            return {"period_clamped": False}
        return {
            "period_clamped": True,
            "message": f"Период сокращён сервером до последних {MAX_PERIOD_DAYS} дней.",
        }

    @staticmethod
    def _in_period(value: str, start: date, end: date) -> bool:
        try:
            current: date = date.fromisoformat(value[:10])
        except ValueError:
            return False
        return start <= current <= end

    def _read_stage_history(self, args: dict[str, Any]) -> dict[str, Any]:
        period = self._parse_period(args, {"date_from", "date_to"})
        if isinstance(period, str):
            return self._error(period)
        if self._task is None:
            return self._denied()
        start, end, clamped = period
        events: list[dict[str, Any]] = [
            {
                "source_id": f"history:{self.task_id}:{event.get('id', index)}",
                "at": event.get("at"),
                "from": event.get("from"),
                "to": event.get("to"),
            }
            for index, event in enumerate(self._task.get("stage_history", []), start=1)
            if self._in_period(str(event.get("at", "")), start, end)
        ]
        source_id: str = f"history:{self.task_id}"
        if not events:
            return self._empty(source_id, "За выбранный период переходов нет.")
        return {
            "status": "ok",
            "source_id": source_id,
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            **self._clamp_note(clamped),
            "complete": bool(self._task.get("history_complete", True)),
            "events": events,
        }

    def _read_comments(self, args: dict[str, Any]) -> dict[str, Any]:
        period = self._parse_period(args, {"date_from", "date_to", "max_fragments"})
        if isinstance(period, str):
            return self._error(period)
        requested: Any = args.get("max_fragments")
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
            return self._error("max_fragments должен быть положительным целым.")
        if self._task is None:
            return self._denied()
        start, end, clamped = period
        comments: list[dict[str, Any]] = [
            comment
            for comment in self._task.get("comments", [])
            if self._in_period(str(comment.get("at", "")), start, end)
        ]
        source_id: str = f"comments:{self.task_id}"
        if not comments:
            return self._empty(source_id, "За выбранный период комментариев нет.")
        limit: int = min(requested, SERVER_COMMENT_CAP)
        selected: list[dict[str, Any]] = comments[-limit:]
        fragments: list[dict[str, Any]] = [
            {
                "source_id": f"comment:{self.task_id}:{comment['id']}",
                "at": comment.get("at"),
                "author": self.fixture.display_name(comment.get("author_id")),
                "text": comment.get("text"),
            }
            for comment in selected
        ]
        return {
            "status": "ok",
            "source_id": source_id,
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            **self._clamp_note(clamped),
            "requested": requested,
            "returned": len(fragments),
            "total_in_period": len(comments),
            "truncated": len(comments) > len(fragments),
            "selection": "latest",
            "fragments": fragments,
        }

    def _read_worklog(self, args: dict[str, Any]) -> dict[str, Any]:
        period = self._parse_period(args, {"date_from", "date_to"})
        if isinstance(period, str):
            return self._error(period)
        if self._task is None:
            return self._denied()
        start, end, clamped = period
        entries: list[dict[str, Any]] = [
            {
                "source_id": f"worklog:{self.task_id}:{entry.get('id', index)}",
                "at": entry.get("at"),
                "hours": entry.get("hours"),
                "user": self.fixture.display_name(entry.get("user_id")),
                "note": entry.get("note"),
            }
            for index, entry in enumerate(self._task.get("worklogs", []), start=1)
            if self._in_period(str(entry.get("at", "")), start, end)
        ]
        source_id: str = f"worklog:{self.task_id}"
        if not entries:
            return self._empty(source_id, "За выбранный период трудозатрат нет.")
        total: float = round(sum(float(entry["hours"] or 0) for entry in entries), 2)
        return {
            "status": "ok",
            "source_id": source_id,
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            **self._clamp_note(clamped),
            "total_hours": total,
            "entry_count": len(entries),
            "entries": entries,
        }


def collect_allowed_sources(results: list[dict[str, Any]]) -> set[str]:
    """Collect every source identifier actually returned to the model."""

    allowed: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            source_id: Any = value.get("source_id")
            if isinstance(source_id, str) and source_id:
                allowed.add(source_id)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for result in results:
        visit(result)
    return allowed
