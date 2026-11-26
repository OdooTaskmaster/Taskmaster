"""Recompute AG-01 scores from a saved run without new API calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from contract import validate_answer
from eval_core import collect_allowed_sources
from reporting import write_summary
from runner import load_cases


def _read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def _revalidate(record: dict[str, Any], cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    case: dict[str, Any] | None = cases.get(str(record["case_id"]))
    if case is None:
        raise SystemExit(f"Кейс {record['case_id']} отсутствует в cases.jsonl")
    tool_results: list[dict[str, Any]] = list(record.get("tool_results", []))
    validation = validate_answer(
        record.get("answer"),
        allowed_sources=collect_allowed_sources(tool_results),
        expected=case,
        called_tools=list(record.get("called_tools", [])),
        tool_error_count=sum(
            1
            for name, result in zip(
                record.get("called_tools", []), tool_results
            )
            if result.get("status") == "error"
            and name not in case.get("tool_errors", {})
        ),
    )
    answer: Any = record.get("answer")
    record["score"] = validation.score
    record["validation"] = validation.to_dict()
    record["status"] = answer.get("status") if isinstance(answer, dict) else "error"
    return record


def main() -> int:
    """Rewrite scores and summaries for one run directory."""

    parser = argparse.ArgumentParser(description="Пересчитать оценки сохранённого прогона.")
    parser.add_argument("run_dir", help="Каталог прогона, например results/full-v1")
    parser.add_argument(
        "--patch",
        action="append",
        default=[],
        help="Каталог дополнительного прогона: записи заменяются по (model, case_id).",
    )
    args = parser.parse_args()

    base_dir: Path = Path(__file__).resolve().parent
    run_dir: Path = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = base_dir / run_dir
    raw_path: Path = run_dir / "raw.jsonl"
    if not raw_path.exists():
        print(f"Нет файла {raw_path}")
        return 1

    cases: dict[str, dict[str, Any]] = {
        str(case["id"]): case for case in load_cases(base_dir / "cases.jsonl")
    }
    records: dict[tuple[str, str], dict[str, Any]] = {
        (str(item["model"]), str(item["case_id"])): item
        for item in _read_records(raw_path)
    }
    order: list[tuple[str, str]] = list(records)
    for patch in args.patch:
        patch_dir: Path = Path(patch)
        if not patch_dir.is_absolute():
            patch_dir = base_dir / patch_dir
        for item in _read_records(patch_dir / "raw.jsonl"):
            key = (str(item["model"]), str(item["case_id"]))
            if key not in records:
                order.append(key)
            records[key] = item

    updated: list[dict[str, Any]] = [
        _revalidate(records[key], cases) for key in order
    ]
    with raw_path.open("w", encoding="utf-8") as handle:
        for record in updated:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    write_summary(run_dir, updated)
    print(f"Пересчитано записей: {len(updated)}; каталог: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
