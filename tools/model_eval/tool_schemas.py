"""OpenAI-compatible tool schemas for the AG-01 evaluation."""

from __future__ import annotations

from typing import Any


PERIOD_PROPERTIES: dict[str, Any] = {
    "date_from": {
        "type": "string",
        "description": "Начало периода в формате YYYY-MM-DD включительно.",
    },
    "date_to": {
        "type": "string",
        "description": "Конец периода в формате YYYY-MM-DD включительно.",
    },
}


def build_tool_schemas() -> list[dict[str, Any]]:
    """Return the five narrow read tools from the approved T1 contract."""

    return [
        {
            "type": "function",
            "function": {
                "name": "read_task_card",
                "description": (
                    "Прочитать карточку выбранной задачи: название, состояние, срок, "
                    "исполнитель и идентификатор источника. Параметров нет."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_task_description",
                "description": (
                    "Прочитать доступный фрагмент описания выбранной задачи. "
                    "Сервер сам ограничивает длину и помечает усечение. Параметров нет."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_task_stage_history",
                "description": (
                    "Прочитать наблюдаемые переходы статуса или этапа выбранной задачи "
                    "за период. Неполная история помечается явно."
                ),
                "parameters": {
                    "type": "object",
                    "properties": PERIOD_PROPERTIES,
                    "required": ["date_from", "date_to"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_task_comments",
                "description": (
                    "Прочитать доступные фрагменты комментариев выбранной задачи за период. "
                    "Сервер возвращает не больше своего предела и помечает выборку как неполную."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **PERIOD_PROPERTIES,
                        "max_fragments": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "description": "Сколько последних фрагментов за период запросить.",
                        },
                    },
                    "required": ["date_from", "date_to", "max_fragments"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_task_worklog",
                "description": (
                    "Прочитать сводку трудозатрат по выбранной задаче за период: "
                    "часы, охват и идентификаторы оснований."
                ),
                "parameters": {
                    "type": "object",
                    "properties": PERIOD_PROPERTIES,
                    "required": ["date_from", "date_to"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def build_answer_schema() -> dict[str, Any]:
    """Return the strict JSON schema expected from the model."""

    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["ok", "insufficient_data", "access_denied", "source_error"],
            },
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "field": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["source_id", "field", "value"],
                    "additionalProperties": False,
                },
            },
            "sources": {"type": "array", "items": {"type": "string"}},
            "missing": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["answer", "status", "facts", "sources", "missing"],
        "additionalProperties": False,
    }
