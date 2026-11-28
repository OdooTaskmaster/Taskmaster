"""Measure the opt-in synthetic summary worker interval for BL-02.11."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
import sqlite3
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from types import ModuleType
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from odoo.api import Environment

_OPERATION_PREFIX: str = "summary-timing-"
_ADDON_PACKAGE: str = Path(__file__).resolve().parents[1].name


def _load(name: str) -> ModuleType:
    """Import a taskmaster module from the installed add-on or the test root.

    Example: _load('adapters.odoo_background') returns the worker adapter.
    """
    candidates: tuple[str, ...] = (f"odoo.addons.{_ADDON_PACKAGE}.{name}", name)
    missing: ModuleNotFoundError | None = None
    for candidate in candidates:
        try:
            return import_module(candidate)
        except ModuleNotFoundError as error:
            if error.name is None or not candidate.startswith(error.name):
                raise
            missing = error
    if missing is None:
        raise ModuleNotFoundError(name)
    raise missing


def _validate(
    env: "Environment", expected_db: str, user_id: int, project_id: int,
    run_count: int, expected_task_count: int,
) -> None:
    """Reject an unbound shell or an implicit fixture and run count.

    Example: _validate(env, 'probe', 7, 3, 5, 24) returns None.
    """
    if not isinstance(expected_db, str) or not expected_db.strip() or env.cr.dbname != expected_db:
        raise ValueError("The shell must be bound to the expected database.")
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A positive ordinary user ID is required.")
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("A positive project ID is required.")
    if type(run_count) is not int or run_count <= 0:
        raise ValueError("An explicit positive run count is required.")
    if type(expected_task_count) is not int or expected_task_count < 0:
        raise ValueError("An explicit nonnegative fixture task count is required.")


def _measure(
    run_once: Callable[[int], object], run_count: int, clock: Callable[[], float] = perf_counter,
) -> tuple[float, ...]:
    """Time only each supplied worker call and return its duration in milliseconds.

    The caller builds fixtures and verifies storage outside run_once so queue
    wait, acceptance and result checks never enter a sample. Example:
    _measure(lambda index: None, 3) returns three short durations.
    """
    samples: list[float] = []
    for index in range(run_count):
        start: float = clock()
        run_once(index)
        samples.append((clock() - start) * 1000.0)
    return tuple(samples)


def _statistics(samples: tuple[float, ...]) -> dict[str, float]:
    """Return the minimum, median and maximum of completed samples.

    Example: _statistics((1.0, 2.0, 6.0)) returns 1.0, 2.0 and 6.0.
    """
    return {"min_ms": min(samples), "median_ms": float(median(samples)), "max_ms": max(samples)}


def run(
    env: "Environment", expected_db: str, *, user_id: int, project_id: int,
    run_count: int, expected_task_count: int, temp_parent: str | Path | None = None,
) -> dict[str, object]:
    """Measure the real worker calculation through the committed SQLite snapshot.

    Require the exact test database, an ordinary recipient, a readable
    synthetic project, an explicit fixture task count and run count. Read the
    fixture once through the worker's own recipient gate, then call the real
    OdooSummaryRunner for each run with a prebuilt running operation. Start
    perf_counter immediately before each runner call, which performs the Odoo
    read, summary build and SQLite save, and stop after the save transaction
    has committed. Queue waiting, operation acceptance and snapshot checks are
    outside the samples. Use only an isolated temporary SQLite file and write
    no Odoo records. Do not apply a threshold or approve a profile.
    Example: run(env, 'probe', user_id=7, project_id=3, run_count=5,
    expected_task_count=24) returns labelled millisecond samples.
    """
    _validate(env, expected_db, user_id, project_id, run_count, expected_task_count)
    background: ModuleType = _load("adapters.odoo_background")
    storage: ModuleType = _load("adapters.sqlite_state_store")
    operations: ModuleType = _load("application.operation_store")

    with background._recipient_environment(env, expected_db, user_id) as recipient:
        observed_tasks: int = len(background.read_summary_tasks(recipient, project_id))
    if observed_tasks != expected_task_count:
        raise AssertionError("The requested fixture task count does not match the readable project.")

    with TemporaryDirectory(prefix="summary-timing-", dir=temp_parent) as directory:
        path: Path = Path(directory) / "summary-timing.sqlite3"
        store = storage.SQLiteStateStore(path)
        runner = background.OdooSummaryRunner(store, env, expected_db)
        now: datetime = datetime.now(UTC)
        jobs: tuple[object, ...] = tuple(
            operations.Operation(
                f"{_OPERATION_PREFIX}{uuid4().hex}", user_id, project_id, user_id,
                operations.OperationState.RUNNING, now, now,
            )
            for _ in range(run_count)
        )
        results: list[str] = []

        def run_once(index: int) -> None:
            """Invoke the worker for one prebuilt operation; return None."""
            results.append(runner(jobs[index]))

        samples: tuple[float, ...] = _measure(run_once, run_count)
        if len(results) != run_count:
            raise AssertionError("The worker did not return one saved result per run.")

        with closing(sqlite3.connect(path)) as connection:
            for job in jobs:
                row: tuple[object, ...] | None = connection.execute(
                    "SELECT count(*) FROM summary WHERE run_id = ?", (job.operation_id,),
                ).fetchone()
                if row != (1,):
                    raise AssertionError("A summary snapshot was not committed to isolated SQLite.")

    statistics: dict[str, float] = _statistics(samples)
    return {
        "interval": "worker_calculation_to_committed_sqlite",
        "clock": "perf_counter",
        "queue_wait_included": False,
        "odoo_writes": False,
        "fixture": {
            "database": expected_db,
            "user_id": user_id,
            "project_id": project_id,
            "task_count": expected_task_count,
        },
        "run_count": run_count,
        "samples_ms": list(samples),
        "min_ms": statistics["min_ms"],
        "median_ms": statistics["median_ms"],
        "max_ms": statistics["max_ms"],
    }
