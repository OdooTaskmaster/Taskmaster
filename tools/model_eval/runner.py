"""Prompt construction and one model/case run for AG-01."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from contract import estimate_cost, extract_json, validate_answer
from eval_core import Fixture, ToolServer, collect_allowed_sources
from routerai_client import RouterAIClient, RouterAIError


CURRENT_DATE = "2026-11-25"


class FatalRunError(RuntimeError):
    """Stop the whole run on authentication or balance errors."""


def load_models(path: Path) -> list[dict[str, Any]]:
    """Load model configurations from JSON."""

    with path.open("r", encoding="utf-8") as handle:
        data: Any = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("models.json должен содержать массив")
    return [dict(item) for item in data if isinstance(item, dict)]


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load JSONL cases."""

    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text: str = line.strip()
            if not text:
                continue
            item: Any = json.loads(text)
            if not isinstance(item, dict):
                raise ValueError(f"cases.jsonl:{line_number}: ожидался объект")
            cases.append(dict(item))
    return cases


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding the environment."""

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text: str = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def repo_root() -> Path:
    """Return the plugin repository root."""

    return Path(__file__).resolve().parents[2]


def build_system_prompt(answer_schema: Mapping[str, Any]) -> str:
    """Build the stable Russian instruction for the pilot contract."""

    return (
        "Ты — ассистент по разбору рисков в Odoo Project. Работай только с выбранной "
        "задачей и только с данными, которые вернули инструменты.\n\n"
        "Правила:\n"
        "1. Не выдумывай значения, идентификаторы, ссылки, причины и названия. "
        "Если данных нет, верни status=insufficient_data и перечисли пробелы в missing.\n"
        "2. Отделяй проверяемые факты от сообщений участников. Фраза в комментарии — "
        "это утверждение участника, а не доказанная причина; её можно объяснить в answer, "
        "но не выдавать за факт.\n"
        "3. Если сервер вернул status=denied — это отказ доступа. Верни "
        "status=access_denied, пустые facts и sources и не раскрывай название, детали, "
        "счётчики или существование записи.\n"
        "4. Если сервер вернул status=empty — это проверенная пустота. Если status=error — "
        "это сбой источника; верни source_error, если без него ответить нельзя.\n"
        "5. Если результат помечен truncated=true, не выдавай фрагмент за полный. "
"Период чтения ограничен 90 днями: широкий диапазон сервер "
"сокращает сам и помечает period_clamped=true. Передавай "
"только параметры из схемы инструмента: лишние "
"параметры отклоняются.\n"
        "6. В facts указывай только source_id, field и value из полученных результатов. "
        "В sources — только реально полученные source_id. Примеры: task:T-101, "
        "description:T-101, history:T-101, comment:T-101:C3, worklog:T-101.\n"
        "7. Не вызывай один и тот же инструмент с теми же аргументами повторно. "
        "Не передавай ID проекта, задачи или пользователя: сервер подставляет их сам.\n"
        "8. Ответь на русском, кратко и по делу. Верни ТОЛЬКО JSON-объект по схеме, "
        "без markdown и пояснений вокруг.\n\n"
        f"Схема ответа: {json.dumps(answer_schema, ensure_ascii=False)}"
    )


def build_initial_message(case: Mapping[str, Any], fixture: Fixture) -> str:
    """Build the minimal signal; the model must read details through tools."""

    user = fixture.user(str(case["user_id"]))
    role: str = str(user.get("role", "unknown")) if user else "unknown"
    access: bool = fixture.can_access(str(case["user_id"]), str(case["task_id"]))
    access_line: str = (
        "Доступ к выбранной задаче проверяет сервер."
        if access
        else "Доступ к выбранной задаче не подтверждён; сервер проверит его при чтении."
    )
    return (
        f"Проект: {fixture.project['name']}.\n"
        f"Выбранная задача: {case['task_id']}.\n"
        f"Текущая дата: {CURRENT_DATE}.\n"
        f"Роль пользователя: {role}.\n"
        f"{access_line}\n"
        "Карта доступных данных: карточка, описание, история статуса, комментарии, "
        "трудозатраты.\n"
        f"Вопрос: {case['question']}"
    )


def build_payload(
    model: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[Mapping[str, Any]] | None,
    answer_schema: Mapping[str, Any],
    max_tokens: int,
    response_format: bool,
) -> dict[str, Any]:
    """Build one Chat Completions payload with pinned provider routing."""

    tokens_field: str = str(model.get("max_tokens_field", "max_tokens"))
    payload: dict[str, Any] = {
        "model": model["id"],
        "messages": list(messages),
        tokens_field: max_tokens,
        "provider": {"only": [model["endpoint"]], "allow_fallbacks": False},
    }
    supported: set[str] = set(model.get("supported_parameters", []))
    if "temperature" in supported:
        payload["temperature"] = 0.0
    if "seed" in supported:
        payload["seed"] = 42
    if tools:
        payload["tools"] = list(tools)
        payload["tool_choice"] = "auto"
    if response_format and model.get("supports_structured"):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "agent_answer",
                "strict": True,
                "schema": dict(answer_schema),
            },
        }
    return payload


def _usage(raw: Mapping[str, Any]) -> dict[str, int]:
    usage: Any = raw.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _cost(model: Mapping[str, Any], usage: Mapping[str, int]) -> float:
    pricing: Mapping[str, Any] = model["pricing_per_token"]
    return round(
        usage["prompt_tokens"] * float(pricing["prompt"])
        + usage["completion_tokens"] * float(pricing["completion"]),
        6,
    )


def _assistant_message(raw: Mapping[str, Any]) -> dict[str, Any]:
    choices: Any = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RouterAIError(200, "в ответе нет choices")
    choice: Any = choices[0]
    message: Any = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise RouterAIError(200, "в ответе нет message")
    return message


def _assistant_for_history(message: Mapping[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    tool_calls: Any = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        item["tool_calls"] = tool_calls
    return item


def _parse_arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def run_case(
    *,
    model: Mapping[str, Any],
    case: Mapping[str, Any],
    fixture: Fixture,
    client: RouterAIClient,
    tool_schemas: Sequence[Mapping[str, Any]],
    answer_schema: Mapping[str, Any],
    max_steps: int,
    max_tokens: int,
    repair: bool,
) -> dict[str, Any]:
    """Run one model on one case, executing tools locally and logging everything."""

    server = ToolServer(
        fixture=fixture,
        task_id=str(case["task_id"]),
        user_id=str(case["user_id"]),
        tool_errors=dict(case.get("tool_errors", {})),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(answer_schema)},
        {"role": "user", "content": build_initial_message(case, fixture)},
    ]
    tool_results: list[dict[str, Any]] = []
    tool_calls_log: list[dict[str, Any]] = []
    called_tools: list[str] = []
    total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    total_cost: float = 0.0
    total_latency_ms: int = 0
    steps: int = 0
    repair_used: bool = False
    error_message: str | None = None
    parsed: dict[str, Any] | None = None
    final_content: Any = None

    def call(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal total_cost, total_latency_ms
        started: float = time.perf_counter()
        raw: dict[str, Any] = client.chat(payload)
        latency_ms: int = int((time.perf_counter() - started) * 1000)
        usage: dict[str, int] = _usage(raw)
        total_latency_ms += latency_ms
        for key in total_usage:
            total_usage[key] += usage[key]
        total_cost += _cost(model, usage)
        return raw

    try:
        for step in range(max_steps):
            steps += 1
            raw = call(
                build_payload(
                    model,
                    messages,
                    tools=tool_schemas,
                    answer_schema=answer_schema,
                    max_tokens=max_tokens,
                    response_format=False,
                )
            )
            message: dict[str, Any] = _assistant_message(raw)
            messages.append(_assistant_for_history(message))
            tool_calls: Any = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                for index, tool_call in enumerate(tool_calls):
                    if not isinstance(tool_call, dict):
                        continue
                    function: Any = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    # Некоторые провайдеры gpt-oss не срезают harmony-маркеры канала
                    # из имени функции: read_task_card<|channel|>commentary.
                    name: str = str(function.get("name", "")).split("<|")[0].strip()
                    arguments: dict[str, Any] | None = _parse_arguments(function.get("arguments"))
                    call_id: str = str(tool_call.get("id") or f"call_{step}_{index}")
                    if arguments is None:
                        result: dict[str, Any] = {
                            "status": "error",
                            "source_id": None,
                            "message": "Аргументы инструмента не являются JSON-объектом.",
                        }
                    else:
                        result = server.execute(name, arguments)
                    called_tools.append(name)
                    tool_results.append(result)
                    tool_calls_log.append(
                        {"name": name, "arguments": arguments, "result": result}
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue
            final_content = message.get("content")
            parsed = extract_json(final_content)
            if parsed is not None:
                break
            if repair and model.get("supports_structured"):
                repair_used = True
                steps += 1
                messages.append(
                    {
                        "role": "user",
                        "content": "Предыдущий ответ не соответствует схеме. Верни только "
                        "итоговый JSON-объект по схеме, без markdown.",
                    }
                )
                raw = call(
                    build_payload(
                        model,
                        messages,
                        tools=None,
                        answer_schema=answer_schema,
                        max_tokens=max_tokens,
                        response_format=True,
                    )
                )
                message = _assistant_message(raw)
                messages.append(_assistant_for_history(message))
                final_content = message.get("content")
                parsed = extract_json(final_content)
            break

        if parsed is None and error_message is None:
            steps += 1
            messages.append(
                {
                    "role": "user",
                    "content": "Лимит шагов чтения исчерпан. Верни только итоговый "
                    "JSON-объект по схеме, без вызова инструментов.",
                }
            )
            raw = call(
                build_payload(
                    model,
                    messages,
                    tools=None,
                    answer_schema=answer_schema,
                    max_tokens=max_tokens,
                    response_format=bool(model.get("supports_structured")),
                )
            )
            message = _assistant_message(raw)
            messages.append(_assistant_for_history(message))
            final_content = message.get("content")
            parsed = extract_json(final_content)
    except RouterAIError as error:
        if error.status_code in (401, 402):
            raise FatalRunError(str(error)) from error
        error_message = str(error)

    allowed_sources: set[str] = collect_allowed_sources(tool_results)
    validation = validate_answer(
        parsed,
        allowed_sources=allowed_sources,
        expected=case,
        called_tools=called_tools,
        tool_error_count=sum(
            1
            for name, result in zip(called_tools, tool_results)
            if result.get("status") == "error"
            and name not in case.get("tool_errors", {})
        ),
    )
    return {
        "case_id": case["id"],
        "model": model["id"],
        "model_label": model["label"],
        "endpoint": model["endpoint"],
        "country": model["country"],
        "status": parsed.get("status") if parsed else "error",
        "score": validation.score,
        "validation": validation.to_dict(),
        "answer": parsed,
        "final_content": final_content,
        "tool_calls": tool_calls_log,
        "tool_results": tool_results,
        "called_tools": called_tools,
        "steps": steps,
        "repair_used": repair_used,
        "usage": total_usage,
        "cost_rub": round(total_cost, 6),
        "latency_ms": total_latency_ms,
        "error": error_message,
        "messages": messages,
    }


def estimate_for_selection(
    models: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    *,
    input_tokens: int = 15000,
    output_tokens: int = 1800,
) -> list[dict[str, Any]]:
    """Estimate cost for the selected models and cases."""

    return estimate_cost(
        models,
        len(cases),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
