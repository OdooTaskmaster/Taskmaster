"""Summaries, run metadata, and offline self-checks for AG-01."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from eval_core import Fixture, ToolServer
from tool_schemas import build_answer_schema, build_tool_schemas


def _percent(part: float, total: float) -> str:
    if total <= 0:
        return "0%"
    return f"{round(100 * part / total)}%"


def summarize(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate per-model metrics for the summary table."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["model"])].append(record)

    rows: list[dict[str, Any]] = []
    for model, items in grouped.items():
        count: int = len(items)
        validations: list[Mapping[str, Any]] = [item["validation"] for item in items]
        rows.append(
            {
                "model": model,
                "label": items[0].get("model_label", model),
                "cases": count,
                "score": round(sum(float(item["score"]) for item in items) / count, 1),
                "contract": _percent(
                    sum(1 for item in validations if item.get("contract_ok")), count
                ),
                "status": _percent(
                    sum(1 for item in validations if item.get("status_ok")), count
                ),
                "facts": _percent(
                    sum(float(item.get("facts_score", 0)) for item in validations),
                    35.0 * count,
                ),
                "sources": _percent(
                    sum(float(item.get("sources_score", 0)) for item in validations),
                    20.0 * count,
                ),
                "language": _percent(
                    sum(float(item.get("language_score", 0)) for item in validations),
                    10.0 * count,
                ),
                "cost_rub": round(sum(float(item.get("cost_rub", 0)) for item in items), 3),
                "latency_p50_ms": int(
                    median([int(item.get("latency_ms", 0)) for item in items])
                )
                if items
                else 0,
                "tool_calls": round(
                    sum(len(item.get("called_tools", [])) for item in items) / count, 1
                ),
                "hard_fails": sum(
                    1 for item in validations if item.get("hard_fail")
                ),
            }
        )
    rows.sort(key=lambda row: float(row["score"]), reverse=True)
    return rows


def write_summary(run_dir: Path, records: Sequence[Mapping[str, Any]]) -> None:
    """Write CSV and Markdown summaries next to the raw JSONL."""

    rows: list[dict[str, Any]] = summarize(records)
    with (run_dir / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)

    lines: list[str] = [
        "# AG-01: результаты прогона",
        "",
        "Оценка автоматическая: контракт 20, факты 35, источники 20, статус 15, русский 10.",
        "Смысловая проверка формулировок и различия «факт / сообщение участника» — в ручном ревью.",
        "",
        "| Модель | Кейсов | Оценка | Контракт | Статус | Факты | Источники | Русский | ₽ | p50, мс | Вызовов | Hard fail |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {label} | {cases} | {score} | {contract} | {status} | {facts} | "
            "{sources} | {language} | {cost_rub} | {latency_p50_ms} | {tool_calls} | "
            "{hard_fails} |".format(**row)
        )
    lines.extend(["", "## Кейсы", "", "| Модель | Кейс | Оценка | Статус | Ошибки |", "|---|---|---:|---|---|"])
    for record in records:
        notes: str = "; ".join(record["validation"].get("notes", [])) or "—"
        lines.append(
            f"| {record.get('model_label', record['model'])} | {record['case_id']} | "
            f"{record['score']} | {record.get('status')} | {notes} |"
        )
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_metadata(
    run_dir: Path,
    *,
    run_id: str,
    stage: str,
    models: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    max_cost: float,
    estimate: Sequence[Mapping[str, Any]],
    git_commit: str,
) -> None:
    """Write run metadata without secrets."""

    data: dict[str, Any] = {
        "run_id": run_id,
        "stage": stage,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "git_commit": git_commit,
        "max_cost_rub": max_cost,
        "models": [model["id"] for model in models],
        "cases": [case["id"] for case in cases],
        "estimate_rub": list(estimate),
    }
    (run_dir / "run.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def run_self_test(fixture_path: Path, cases_path: Path) -> list[str]:
    """Check fixture shape, tool contracts, and case definitions without network."""

    fixture = Fixture.load(fixture_path)
    cases: list[dict[str, Any]] = []
    with cases_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(json.loads(line))

    errors: list[str] = []
    tasks: list[dict[str, Any]] = fixture.data.get("tasks", [])
    if len(tasks) != 15:
        errors.append(f"ожидалось 15 задач, найдено {len(tasks)}")
    comment_count: int = sum(len(task.get("comments", [])) for task in tasks)
    if not 25 <= comment_count <= 40:
        errors.append(f"комментариев {comment_count}, ожидалось 25–40")
    if len(cases) != 10:
        errors.append(f"кейсов {len(cases)}, ожидалось 10")
    if not fixture.can_access("user_lead", "T-108"):
        errors.append("руководитель должен видеть T-108")
    if fixture.can_access("user_analyst", "T-108"):
        errors.append("аналитик не должен видеть T-108")

    server = ToolServer(fixture=fixture, task_id="T-101", user_id="user_lead")
    card: dict[str, Any] = server.execute("read_task_card", {})
    if card.get("task", {}).get("deadline") != "2026-11-20":
        errors.append("карточка T-101: неверный срок")
    description: dict[str, Any] = server.execute("read_task_description", {})
    if not description.get("truncated"):
        errors.append("описание T-101 должно быть усечено")
    if "архив старше 2023" in str(description.get("text", "")):
        errors.append("фраза про архив не должна попадать в первый фрагмент")
    comments: dict[str, Any] = server.execute(
        "read_task_comments",
        {"date_from": "2026-11-01", "date_to": "2026-11-25", "max_fragments": 2},
    )
    if comments.get("returned") != 2 or not comments.get("truncated"):
        errors.append("комментарии должны упираться в серверный предел 5")
    worklog: dict[str, Any] = server.execute(
        "read_task_worklog", {"date_from": "2026-11-01", "date_to": "2026-11-25"}
    )
    if worklog.get("total_hours") != 12.5:
        errors.append("трудозатраты T-101: ожидалось 12.5")

    denied = ToolServer(fixture=fixture, task_id="T-108", user_id="user_analyst")
    denied_card: dict[str, Any] = denied.execute("read_task_card", {})
    if denied_card.get("status") != "denied":
        errors.append("T-108 для аналитика должен быть denied")
    if "title" in denied_card or denied_card.get("source_id"):
        errors.append("отказ не должен раскрывать название или source_id")

    schemas = build_tool_schemas()
    if len(schemas) != 5:
        errors.append("должно быть ровно 5 инструментов")
    if build_answer_schema().get("required") != ["answer", "status", "facts", "sources", "missing"]:
        errors.append("схема ответа не совпадает с контрактом")
    return errors
