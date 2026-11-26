"""CLI for the AG-01 external model evaluation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from contract import estimate_cost
from eval_core import Fixture
from reporting import run_self_test, write_metadata, write_summary
from routerai_client import RouterAIClient
from runner import (
    FatalRunError,
    build_initial_message,
    build_system_prompt,
    estimate_for_selection,
    load_cases,
    load_env_file,
    load_models,
    repo_root,
    run_case,
)
from tool_schemas import build_answer_schema, build_tool_schemas


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
STAGES: dict[str, dict[str, Any]] = {
    "probe": {
        "models": ["deepseek/deepseek-v4.1-flash-1017"],
        "cases": ["C01"],
        "max_cost": 10.0,
    },
    "smoke": {
        "models": None,
        "cases": ["C01", "C04", "C08"],
        "max_cost": 80.0,
    },
    "full": {
        "models": None,
        "cases": None,
        "max_cost": 100.0,
    },
}


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""

    parser = argparse.ArgumentParser(description="AG-01: сравнение внешних моделей через RouterAI.")
    parser.add_argument("--stage", choices=sorted(STAGES), default="probe")
    parser.add_argument("--models", help="ID моделей через запятую; по умолчанию — набор стадии.")
    parser.add_argument("--cases", help="ID кейсов через запятую; по умолчанию — набор стадии.")
    parser.add_argument("--max-cost", type=float, default=None, help="Жёсткий лимит расходов, руб..")
    parser.add_argument("--max-steps", type=int, default=6, help="Максимум шагов чтения.")
    parser.add_argument("--max-tokens", type=int, default=2000, help="Лимит выхода на один запрос.")
    parser.add_argument("--timeout", type=int, default=90, help="Таймаут одного HTTP-запроса, сек.")
    parser.add_argument("--run-id", default=None, help="Имя каталога результатов.")
    parser.add_argument("--env-file", default=None, help="Путь к .env с ROUTERAI_API_KEY.")
    parser.add_argument("--base-url", default="https://routerai.ru/api/v1")
    parser.add_argument("--no-repair", action="store_true", help="Не делать repair-вызов JSON Schema.")
    parser.add_argument("--dry-run", action="store_true", help="Показать промпт и смету без запросов.")
    parser.add_argument("--estimate", action="store_true", help="Показать только смету.")
    parser.add_argument("--self-test", action="store_true", help="Проверить стенд без сети.")
    parser.add_argument("--list", action="store_true", help="Показать модели и кейсы.")
    parser.add_argument("--force", action="store_true", help="Запустить, даже если смета выше лимита.")
    return parser.parse_args()


def _select_by_ids(
    items: Sequence[dict[str, Any]], raw: str | Sequence[str] | None
) -> list[dict[str, Any]]:
    if raw is None:
        return list(items)
    if isinstance(raw, str):
        wanted: list[str] = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        wanted = [str(part).strip() for part in raw if str(part).strip()]
    by_id: dict[str, dict[str, Any]] = {str(item["id"]): item for item in items}
    missing: list[str] = [item for item in wanted if item not in by_id]
    if missing:
        raise SystemExit(f"Неизвестные ID: {', '.join(missing)}")
    return [by_id[item] for item in wanted]


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root(),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _print_estimate(rows: Sequence[dict[str, Any]]) -> None:
    print("Смета (15k входа + 1.8k выхода на кейс, худший случай):")
    for row in rows:
        print(f"  {row['model']:<42} {row['cases']:>2} кейс(ов)  ~{row['estimated_rub']:>7.2f} руб.")
    print(f"  Итого: ~{sum(float(row['estimated_rub']) for row in rows):.2f} руб.")


def _print_list(models: Sequence[dict[str, Any]], cases: Sequence[dict[str, Any]]) -> None:
    print("Модели:")
    for model in models:
        pricing: dict[str, Any] = model["pricing_per_token"]
        print(
            f"  {model['id']:<42} {model['endpoint']:<12} {model['country']:<2} "
            f"{float(pricing['prompt']) * 1_000_000:>7.1f}/"
            f"{float(pricing['completion']) * 1_000_000:>8.1f} руб./1M"
        )
    print("Кейсы:")
    for case in cases:
        print(f"  {case['id']}  {case['title']}")


def main() -> int:
    """Run the requested stage."""

    args = parse_args()
    fixture_path: Path = BASE_DIR / "fixtures" / "project.json"
    cases_path: Path = BASE_DIR / "cases.jsonl"
    models_path: Path = BASE_DIR / "models.json"

    if args.self_test:
        errors: list[str] = run_self_test(fixture_path, cases_path)
        if errors:
            print("self-test: FAIL")
            for error in errors:
                print(f"  - {error}")
            return 1
        print("self-test: OK")
        return 0

    models: list[dict[str, Any]] = load_models(models_path)
    cases: list[dict[str, Any]] = load_cases(cases_path)
    fixture: Fixture = Fixture.load(fixture_path)

    if args.list:
        _print_list(models, cases)
        return 0

    stage: dict[str, Any] = STAGES[args.stage]
    selected_models: list[dict[str, Any]] = _select_by_ids(
        models, args.models if args.models is not None else stage["models"]
    )
    selected_cases: list[dict[str, Any]] = _select_by_ids(
        cases, args.cases if args.cases is not None else stage["cases"]
    )
    if args.stage == "full" and args.models is None:
        print("Для full укажите --models: полный прогон всех моделей слишком дорог.")
        return 2

    max_cost: float = float(args.max_cost if args.max_cost is not None else stage["max_cost"])
    estimate: list[dict[str, Any]] = estimate_for_selection(selected_models, selected_cases)
    _print_estimate(estimate)
    if args.estimate:
        return 0

    if args.dry_run:
        print("\n--- SYSTEM PROMPT ---")
        print(build_system_prompt(build_answer_schema()))
        print("\n--- FIRST CASE MESSAGE ---")
        print(build_initial_message(selected_cases[0], fixture))
        print(f"\nМодели: {', '.join(model['id'] for model in selected_models)}")
        print(f"Кейсы: {', '.join(case['id'] for case in selected_cases)}")
        return 0

    estimated_total: float = sum(float(row["estimated_rub"]) for row in estimate)
    if estimated_total > max_cost and not args.force:
        print(
            f"Смета {estimated_total:.2f} руб. выше лимита {max_cost:.2f} руб.. "
            "Уменьшите набор или добавьте --force (жёсткий лимит всё равно сработает)."
        )
        return 3

    env_path: Path = Path(args.env_file) if args.env_file else repo_root() / ".env"
    load_env_file(env_path)
    api_key: str = os.environ.get("ROUTERAI_API_KEY", "").strip()
    if not api_key:
        print(f"Нет ROUTERAI_API_KEY. Заполните: {env_path}")
        return 4

    run_id: str = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir: Path = BASE_DIR / "results" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_metadata(
        run_dir,
        run_id=run_id,
        stage=args.stage,
        models=selected_models,
        cases=selected_cases,
        max_cost=max_cost,
        estimate=estimate,
        git_commit=_git_commit(),
    )
    raw_path: Path = run_dir / "raw.jsonl"
    records: list[dict[str, Any]] = []
    spent: float = 0.0
    stopped: bool = False
    fatal: str | None = None
    client = RouterAIClient(api_key, base_url=args.base_url, timeout_seconds=args.timeout)
    tool_schemas = build_tool_schemas()
    answer_schema = build_answer_schema()

    try:
        for model in selected_models:
            for case in selected_cases:
                print(f"[{model['label']}] {case['id']} ...", flush=True)
                try:
                    record = run_case(
                        model=model,
                        case=case,
                        fixture=fixture,
                        client=client,
                        tool_schemas=tool_schemas,
                        answer_schema=answer_schema,
                        max_steps=args.max_steps,
                        max_tokens=int(model.get("max_tokens_override", args.max_tokens)),
                        repair=not args.no_repair,
                    )
                except FatalRunError as error:
                    fatal = str(error)
                    stopped = True
                    break
                records.append(record)
                spent += float(record["cost_rub"])
                with raw_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(
                    f"  score={record['score']:.1f} status={record['status']} "
                    f"cost={record['cost_rub']:.4f} руб. latency={record['latency_ms']} ms"
                )
                if spent >= max_cost:
                    print(f"Достигнут лимит {max_cost:.2f} руб.: остановка.")
                    stopped = True
                    break
            if stopped:
                break
    finally:
        client.close()

    if records:
        write_summary(run_dir, records)
    print(f"Результаты: {run_dir}")
    print(f"Потрачено: {spent:.4f} руб.; кейсов: {len(records)}")
    if fatal:
        print(f"Остановка: {fatal}")
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
