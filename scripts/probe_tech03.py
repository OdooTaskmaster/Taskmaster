"""Measure synthetic writes and validate the proposed TECH-03 recovery boundary."""

from __future__ import annotations

import argparse
import multiprocessing
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing, contextmanager
from math import ceil
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier

    from psycopg2.extensions import connection as PgConnection


_ROWS_PER_PROCESS: int = 100
_HOLD_SECONDS: float = 0.005
_BUSY_SECONDS: int = 3
_PAYLOAD: str = "x" * 1024


def _sqlite(path: Path) -> sqlite3.Connection:
    """Open a probe SQLite file with a bounded lock wait and durable commits.

    Example: _sqlite(Path('probe.db')) returns a connection the caller closes.
    """
    connection: sqlite3.Connection = sqlite3.connect(path, timeout=_BUSY_SECONDS)
    connection.execute("PRAGMA synchronous=EXTRA")
    return connection


def _postgres(port: int, user: str) -> PgConnection:
    """Connect only to a dedicated local PostgreSQL probe database.

    Example: _postgres(55433, 'tech03_probe') returns a test connection.
    """
    import psycopg2

    return psycopg2.connect(
        host="127.0.0.1", port=port, dbname="tech03_probe", user=user, connect_timeout=5,
        options=f"-c statement_timeout=10000 -c lock_timeout={_BUSY_SECONDS * 1000}",
    )


def _wait_for_peer(barrier: Barrier) -> None:
    """Release both processes to start together or fail within ten seconds.

    Example: _wait_for_peer(barrier) returns None after both workers arrive.
    """
    barrier.wait(timeout=10)


def _write_rows(path: Path, port: int | None, user: str, table: str, writer: int) -> list[float]:
    """Insert one KiB per transaction for a writer and return commit times in ms.

    Example: _write_rows(path, None, '', 'probe_writes', 1) returns 100 samples.
    """
    sqlite: bool = port is None
    connection: sqlite3.Connection | PgConnection = _sqlite(path) if port is None else _postgres(port, user)
    placeholder: str = "?" if sqlite else "%s"
    statement: str = f"INSERT INTO {table} VALUES ({placeholder}, {placeholder}, {placeholder})"
    samples: list[float] = []
    with closing(connection), closing(connection.cursor()) as cursor:
        for number in range(_ROWS_PER_PROCESS):
            start: float = perf_counter()
            with connection:
                if sqlite:
                    cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(statement, (writer, number, _PAYLOAD))
                sleep(_HOLD_SECONDS)
            samples.append((perf_counter() - start) * 1000)
    return samples


def _measure(path: Path, table: str, port: int | None = None, user: str = "") -> None:
    """Run bounded competing commits and verify that both writers' rows survived.

    Example: _measure(path, 'probe_writes') prints SQLite counts and latency.
    Any worker error, including a lock timeout, fails the probe.
    """
    context = multiprocessing.get_context("spawn")
    barrier: Barrier = context.Barrier(2)
    with ProcessPoolExecutor(2, mp_context=context, initializer=_wait_for_peer, initargs=(barrier,)) as pool:
        first = pool.submit(_write_rows, path, port, user, table, 1)
        second = pool.submit(_write_rows, path, port, user, table, 2)
        samples: list[float] = sorted(first.result(timeout=45) + second.result(timeout=45))

    connection: sqlite3.Connection | PgConnection = _sqlite(path) if port is None else _postgres(port, user)
    with closing(connection), closing(connection.cursor()) as cursor:
        cursor.execute(f"SELECT writer, count(*) FROM {table} GROUP BY writer ORDER BY writer")
        if cursor.fetchall() != [(1, _ROWS_PER_PROCESS), (2, _ROWS_PER_PROCESS)]:
            raise RuntimeError("Incorrect persisted row counts")
        if port is None:
            cursor.execute("PRAGMA integrity_check")
            if cursor.fetchone() != ("ok",):
                raise RuntimeError("SQLite integrity check failed")
    engine: str = "SQLite DELETE" if port is None else "PostgreSQL"
    p95: float = samples[ceil(0.95 * len(samples)) - 1]
    print(f"{engine}: {len(samples)} commits, 0 errors, p50={median(samples):.2f} ms, p95={p95:.2f} ms, max={max(samples):.2f} ms")


@contextmanager
def _probe_schema(port: int, user: str) -> Iterator[str]:
    """Yield an isolated generated PostgreSQL schema and drop it on exit.

    Example: with _probe_schema(55433, 'tech03_probe') as schema: ...
    """
    schema: str = f"probe_{uuid4().hex}"
    with closing(_postgres(port, user)) as connection, closing(connection.cursor()) as cursor:
        with connection:
            cursor.execute(f"CREATE SCHEMA {schema}")
        try:
            yield schema
        finally:
            with connection:
                cursor.execute(f"DROP SCHEMA {schema} CASCADE")


class RollbackPublication(Exception):
    """Inject a write failure before commit in the synthetic test only.

    Example: raise RollbackPublication() to discard the test transaction.
    """


def _publish(port: int, user: str, schema: str, key: str, *, rollback: bool = False) -> int:
    """Reserve the action key and store the synthetic message and receipt atomically.

    Example: _publish(port, user, schema, 'demo:v1') returns the message ID;
    the same key returns that ID without creating another message.
    """
    with closing(_postgres(port, user)) as connection, connection, closing(connection.cursor()) as cursor:
        cursor.execute(
            f"INSERT INTO {schema}.receipt (action_key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING action_key",
            (key,),
        )
        if cursor.fetchone() is None:
            cursor.execute(f"SELECT message_id FROM {schema}.receipt WHERE action_key = %s", (key,))
            return int(cursor.fetchone()[0])
        cursor.execute(f"INSERT INTO {schema}.message (body) VALUES ('synthetic') RETURNING id")
        message_id: int = int(cursor.fetchone()[0])
        cursor.execute(f"UPDATE {schema}.receipt SET message_id = %s WHERE action_key = %s", (message_id, key))
        if rollback:
            raise RollbackPublication()
        return message_id


def _check_recovery(path: Path, port: int, user: str, schema: str) -> None:
    """Verify rollback and replay with a PostgreSQL receipt and SQLite status.

    Example: _check_recovery(path, port, user, schema) prints success only
    after checking message counts and recovery of the original ID.
    """
    key: str = "comment:demo:v1"
    with closing(_postgres(port, user)) as connection, closing(connection.cursor()) as cursor:
        with connection:
            cursor.execute(f"CREATE TABLE {schema}.message (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, body text NOT NULL)")
            cursor.execute(f"CREATE TABLE {schema}.receipt (action_key text PRIMARY KEY, message_id bigint)")
        try:
            _publish(port, user, schema, key, rollback=True)
        except RollbackPublication:
            pass
        with connection:
            cursor.execute(f"SELECT (SELECT count(*) FROM {schema}.message), (SELECT count(*) FROM {schema}.receipt)")
            if cursor.fetchone() != (0, 0):
                raise RuntimeError("Publication did not roll back with its receipt")

        with closing(_sqlite(path)) as state:
            with state:
                state.execute("CREATE TABLE operation (action_key text PRIMARY KEY, message_id int)")
                state.execute("INSERT INTO operation VALUES (?, NULL)", (key,))
            original: int = _publish(port, user, schema, key)
            state.execute("UPDATE operation SET message_id = ? WHERE action_key = ?", (original, key))
            state.rollback()  # Odoo-side commit succeeded; local status commit did not.
            if state.execute("SELECT message_id FROM operation WHERE action_key = ?", (key,)).fetchone() != (None,):
                raise RuntimeError("Expected unapplied local status after the simulated failure")

            with ThreadPoolExecutor(2) as pool:
                retries = [pool.submit(_publish, port, user, schema, key) for _ in range(2)]
                restored: list[int] = [retry.result() for retry in retries]
            if restored != [original, original]:
                raise RuntimeError("A retry did not return the original message ID")
            with state:
                state.execute("UPDATE operation SET message_id = ? WHERE action_key = ?", (restored[0], key))

        with closing(_sqlite(path)) as state:
            if state.execute("SELECT message_id FROM operation WHERE action_key = ?", (key,)).fetchone() != (original,):
                raise RuntimeError("Local status was not restored")
        with connection:
            cursor.execute(f"SELECT (SELECT count(*) FROM {schema}.message), (SELECT count(*) FROM {schema}.receipt)")
            if cursor.fetchone() != (1, 1):
                raise RuntimeError("Replay did not preserve exactly one publication and receipt")
    print("Recovery: 0 messages on rollback, 1 after status loss and two retries; original status restored")


def main() -> None:
    """Run SQLite and optionally PostgreSQL checks, failing on any missing write.

    Example: python -B scripts/probe_tech03.py --pg-port 55433 runs both
    engines and crash recovery against the dedicated local test database.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-dir", type=Path, help="Parent directory on a local filesystem")
    parser.add_argument("--pg-port", type=int, help="Port of an isolated local PostgreSQL on 127.0.0.1")
    parser.add_argument("--pg-user", default="tech03_probe")
    args: argparse.Namespace = parser.parse_args()

    with TemporaryDirectory(prefix="tech03-", dir=args.temp_dir) as directory:
        path: Path = Path(directory) / "probe.sqlite3"
        table_sql: str = "(writer int NOT NULL, seq int NOT NULL, payload text NOT NULL, PRIMARY KEY (writer, seq))"
        with closing(_sqlite(path)) as connection:
            mode: str = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
            if mode != "delete":
                raise RuntimeError("Expected SQLite DELETE journal mode")
            connection.execute(f"CREATE TABLE probe_writes {table_sql}")
        print(f"SQLite version: {sqlite3.sqlite_version}")
        _measure(path, "probe_writes")

        if args.pg_port is None:
            print("PostgreSQL comparison and recovery not run (no --pg-port)")
            return
        with _probe_schema(args.pg_port, args.pg_user) as schema:
            table: str = f"{schema}.probe_writes"
            with closing(_postgres(args.pg_port, args.pg_user)) as connection, connection, closing(connection.cursor()) as cursor:
                cursor.execute("SHOW server_version")
                print(f"PostgreSQL version: {cursor.fetchone()[0]}")
                cursor.execute(f"CREATE TABLE {table} {table_sql}")
            _measure(path, table, args.pg_port, args.pg_user)
            _check_recovery(path, args.pg_port, args.pg_user, schema)


if __name__ == "__main__":
    main()
