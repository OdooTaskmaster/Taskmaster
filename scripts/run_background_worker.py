"""Run the dedicated background summary worker from an Odoo shell."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import tzinfo
from importlib import import_module
import logging
import math
import os
from pathlib import Path
import time
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from odoo.api import Environment

    from adapters.odoo_background import OdooSummaryRunner
    from adapters.rabbitmq import RabbitQueue
    from application.background_operations import BackgroundOperations

AMQP_URL_VARIABLE: str = "TASKMASTER_AMQP_URL"
QUEUE_MAX_LENGTH_VARIABLE: str = "TASKMASTER_QUEUE_MAX_LENGTH"
DEFAULT_IDLE_SECONDS: float = 1.0
DEFAULT_FAILURE_SECONDS: float = 5.0
MAX_FAILURE_SECONDS: float = 60.0
_ADDON_PACKAGE: str = Path(__file__).resolve().parents[1].name

_logger = logging.getLogger("taskmaster.background")


def _load(name: str) -> ModuleType:
    """Import a taskmaster module from the installed add-on or the test root.

    Derive the add-on package from this script's parent folder so the shell
    imports the configured installation instead of a hardcoded name.
    Example: _load('adapters.rabbitmq') returns the broker transport module.
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


def _queue_max_length(environ: Mapping[str, str]) -> int:
    """Read a positive queue limit chosen for the deployment.

    Example: _queue_max_length({'TASKMASTER_QUEUE_MAX_LENGTH': '7'}) returns 7.
    """
    raw: str = (environ.get(QUEUE_MAX_LENGTH_VARIABLE) or "").strip()
    if not raw:
        raise ValueError("Set TASKMASTER_QUEUE_MAX_LENGTH for the background worker.")
    try:
        value: int = int(raw)
    except ValueError:
        raise ValueError("The queue max length must be a positive integer.") from None
    if value <= 0:
        raise ValueError("The queue max length must be a positive integer.")
    return value


def _bounded_seconds(value: float, name: str) -> float:
    """Return a finite nonnegative wait or raise a safe configuration error.

    Example: _bounded_seconds(1.0, 'idle_seconds') returns 1.0.
    """
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number.")
    return float(value)


def _positive_length(value: int) -> int:
    """Return an explicit positive queue limit or raise a safe error.

    Example: _positive_length(7) returns 7.
    """
    if type(value) is not int or value <= 0:
        raise ValueError("The queue max length must be a positive integer.")
    return value


def run_tick(background: "BackgroundOperations", queue: "RabbitQueue", runner: "OdooSummaryRunner") -> bool:
    """Recover at most one pending dispatch, then receive at most one operation.

    Example: run_tick(background, queue, runner) returns True when a message
    was handled and False when the queue was empty.
    """
    background.recover(limit=1)
    return queue.receive(lambda operation_id: background.execute(operation_id, runner))


def _failure_types() -> tuple[type[BaseException], ...]:
    """Return the recoverable broker and state failures for the launcher loop.

    Example: _failure_types() returns (BrokerUnavailable, StateStoreError, StateConflict).
    """
    rabbitmq: ModuleType = _load("adapters.rabbitmq")
    operations: ModuleType = _load("application.operation_store")
    return (rabbitmq.BrokerUnavailable, operations.StateStoreError, operations.StateConflict)


def serve(
    background: "BackgroundOperations", queue: "RabbitQueue", runner: "OdooSummaryRunner", *,
    max_ticks: int | None = None, idle_seconds: float = DEFAULT_IDLE_SECONDS,
    failure_seconds: float = DEFAULT_FAILURE_SECONDS, sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run recover/receive ticks with bounded idle and failure waits.

    Pass max_ticks for a bounded integration run; otherwise the dedicated
    launcher runs until interrupted. Consecutive failures back off up to
    MAX_FAILURE_SECONDS and any successful tick resets the wait. Return the
    number of completed ticks.
    Example: serve(background, queue, runner, max_ticks=1) returns 1.
    """
    if max_ticks is not None and (type(max_ticks) is not int or max_ticks <= 0):
        raise ValueError("max_ticks must be a positive integer.")
    idle: float = _bounded_seconds(idle_seconds, "idle_seconds")
    failure: float = _bounded_seconds(failure_seconds, "failure_seconds")
    failures: tuple[type[BaseException], ...] = _failure_types()
    ticks: int = 0
    failure_wait: float = failure
    while max_ticks is None or ticks < max_ticks:
        wait: float = idle
        try:
            handled: bool = run_tick(background, queue, runner)
        except failures:
            wait = failure_wait
            failure_wait = min(failure_wait * 2.0, MAX_FAILURE_SECONDS)
            _logger.warning("Background worker tick failed; retrying later.")
        else:
            failure_wait = failure
            if handled:
                wait = 0.0
                _logger.info("Background worker handled one operation.")
            else:
                _logger.debug("Background worker found no operation.")
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        if wait > 0:
            sleep(wait)
    return ticks


def run(
    env: "Environment", expected_db: str, *, max_ticks: int | None = None,
    idle_seconds: float = DEFAULT_IDLE_SECONDS, failure_seconds: float = DEFAULT_FAILURE_SECONDS,
    environ: Mapping[str, str] | None = None, project_timezone: tzinfo | None = None,
    max_length: int | None = None,
) -> int:
    """Start the dedicated worker for one trusted database and return its ticks.

    Read the AMQP URL from TASKMASTER_AMQP_URL and never print it. Derive the
    state file and queue from expected_db, then run recover/receive ticks.
    Pass max_length explicitly or set TASKMASTER_QUEUE_MAX_LENGTH.
    Example: run(env, 'pilot', max_ticks=1, max_length=8) performs one integration tick.
    """
    if not isinstance(expected_db, str) or not expected_db.strip():
        raise ValueError("An expected database name is required.")
    if env.cr.dbname != expected_db:
        raise ValueError("The shell must be bound to the expected database.")
    source: Mapping[str, str] = os.environ if environ is None else environ
    url: str = (source.get(AMQP_URL_VARIABLE) or "").strip()
    if not url:
        raise ValueError("Set TASKMASTER_AMQP_URL for the background worker.")
    queue_limit: int = _queue_max_length(source) if max_length is None else _positive_length(max_length)
    try:
        pika: ModuleType = import_module("pika")
        if not callable(getattr(pika, "Connection", None)):
            raise ImportError
    except ImportError:
        raise RuntimeError("Install the dependencies from requirements-worker.txt.") from None
    adapter: ModuleType = _load("adapters.odoo_background")
    store_module: ModuleType = _load("adapters.sqlite_state_store")
    rabbitmq: ModuleType = _load("adapters.rabbitmq")
    operations: ModuleType = _load("application.background_operations")
    path: Path = adapter.database_state_path(expected_db)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    store = store_module.SQLiteStateStore(path)
    queue = rabbitmq.RabbitQueue(url, adapter.queue_name(expected_db), queue_limit)
    background = operations.BackgroundOperations(store, queue.publish)
    runner = adapter.OdooSummaryRunner(store, env, expected_db, project_timezone=project_timezone)
    _logger.info("Background worker started.")
    try:
        return serve(
            background, queue, runner, max_ticks=max_ticks,
            idle_seconds=idle_seconds, failure_seconds=failure_seconds,
        )
    finally:
        queue.close()
        _logger.info("Background worker stopped.")
