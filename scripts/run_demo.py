"""Start a disposable local Odoo demo with synthetic fixtures and a model double."""

from argparse import ArgumentParser, Namespace
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import socket
import subprocess
from subprocess import DEVNULL, PIPE, Popen, run
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
import webbrowser

_HOST: str = "127.0.0.1"
_LOGIN: str = "demo-lead@example.test"
_MODULE_NAME: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_]+")
_CHILD_FLAGS: int = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


class _ModelDouble(BaseHTTPRequestHandler):
    """Answer the local demo's Chat Completions requests without processing task text."""

    def do_POST(self) -> None:
        """Return one labelled demo answer for POST /v1/chat/completions."""
        length: int = int(self.headers.get("Content-Length", "0"))
        if self.path != "/v1/chat/completions" or not 0 < length <= 1_000_000:
            self.send_error(400)
            return
        self.rfile.read(length)
        answer: str = (
            "Demo answer from a local model double. Check the dated facts above; "
            "ask the assignee to confirm the cause and the next step."
        )
        body: bytes = json.dumps({"model": "demo-stub", "choices": [{"message": {
            "content": json.dumps({"text": answer, "citations": []}),
        }}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _wait(port: int, process: Popen | None = None) -> None:
    """Wait for a child to listen locally; _wait(58069) returns on readiness."""
    deadline: float = monotonic() + 30
    while (process is None or process.poll() is None) and monotonic() < deadline:
        try:
            with socket.create_connection((_HOST, port), timeout=0.5):
                return
        except OSError:
            sleep(0.25)
    raise RuntimeError(f"Demo service did not start on port {port}.")


def _wait_cluster(pg_bin: Path, port: int, *, process: Popen | None = None, timeout: float = 90.0) -> None:
    """Wait until the local cluster accepts connections, not just listens.

    A cluster in crash recovery accepts sockets but refuses sessions; polling
    pg_isready avoids racing the install step against recovery.
    Example: _wait_cluster(pg_bin, 55433) returns when accepting queries.
    """
    isready: Path = pg_bin / "pg_isready.exe"
    deadline: float = monotonic() + timeout
    while (process is None or process.poll() is None) and monotonic() < deadline:
        probe: int = run([isready, "-h", _HOST, "-p", str(port)],
                         stdout=DEVNULL, stderr=DEVNULL, check=False).returncode
        if probe == 0:
            return
        sleep(1.0)
    raise RuntimeError("The local PostgreSQL cluster did not become ready in time.")


def main() -> None:
    """Launch the isolated demo; run_demo.py --help lists the local prerequisites."""
    repo: Path = Path(__file__).resolve().parents[1]
    parser: ArgumentParser = ArgumentParser(description=__doc__)
    parser.add_argument("--odoo", type=Path, default=repo.parent / "odoo" / "odoo")
    parser.add_argument("--pg-bin", type=Path, default=Path("C:/Program Files/PostgreSQL/18/bin"))
    parser.add_argument("--pg-data", type=Path, required=True, help="Existing isolated test cluster; must be stopped")
    parser.add_argument("--pg-user", default="tech03_probe")
    parser.add_argument("--pg-port", type=int, default=55433)
    parser.add_argument("--http-port", type=int, default=58069)
    parser.add_argument("--module", default=repo.name,
                        help="Technical module name of this checkout (letters, digits, underscores)")
    parser.add_argument("--no-browser", action="store_true")
    args: Namespace = parser.parse_args()
    if not _MODULE_NAME.fullmatch(args.module):
        parser.error("The module name must be lowercase letters, digits and underscores "
                     "(set --module explicitly for this checkout).")
    if not (args.pg_data / "PG_VERSION").is_file() or not (args.odoo / "odoo-bin").is_file():
        parser.error("Odoo or the PostgreSQL test cluster was not found.")
    postgres: Path = args.pg_bin / "postgres.exe"
    pg_ctl: Path = args.pg_bin / "pg_ctl.exe"
    dropdb: Path = args.pg_bin / "dropdb.exe"
    if not all(path.is_file() for path in (postgres, pg_ctl, dropdb)):
        parser.error("PostgreSQL binaries were not found.")
    if run([pg_ctl, "status", "-D", args.pg_data], stdout=DEVNULL, stderr=DEVNULL).returncode != 3:
        parser.error("Use a stopped, dedicated test cluster.")
    if args.pg_port == args.http_port or not all(1 <= port <= 65535 for port in (args.pg_port, args.http_port)):
        parser.error("Provide two different TCP ports in the range 1–65535.")
    for port in (args.pg_port, args.http_port):
        with socket.socket() as check:
            check.bind((_HOST, port))
    database: str = f"taskmaster_demo_{uuid4().hex[:12]}"
    password: str = token_urlsafe(16)
    child_env: dict[str, str] = os.environ.copy()
    child_env.update({
        "TASKMASTER_DEMO_DB": database, "TASKMASTER_DEMO_PASSWORD": password,
        "TASKMASTER_DEMO_SEED": str(repo / "scripts" / "pilot_demo_project.py"),
        "TASKMASTER_ENABLE_RISK_ANALYSIS": "1", "TASKMASTER_DIALOGUE_RETENTION_DAYS": "1",
        "TASKMASTER_DIALOGUE_WINDOW": "6", "TASKMASTER_MODEL_API_KEY": "synthetic-only",
        "TASKMASTER_MODEL": "demo-stub", "TASKMASTER_MODEL_TIMEOUT_SECONDS": "5",
        "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
    })
    try:
        from langgraph.graph import StateGraph  # noqa: F401
    except ImportError:
        parser.error("LangGraph must be available in the Odoo Python environment for this demo.")
    seed: str = """
import os
from runpy import run_path
from odoo import Command
db = os.environ.pop('TASKMASTER_DEMO_DB')
assert db.startswith('taskmaster_demo_') and db == env.cr.dbname
password = os.environ.pop('TASKMASTER_DEMO_PASSWORD')
logins = ('demo-lead@example.test', 'demo-one@example.test', 'demo-two@example.test')
for index, login in enumerate(logins):
    group = env.ref('project.group_project_manager' if index == 0 else 'project.group_project_user')
    env['res.users'].with_context(no_reset_password=True, tracking_disable=True).create({
        'name': f'Demo user {index + 1}', 'login': login, 'password': password,
        'group_ids': [Command.set([env.ref('base.group_user').id, group.id])],
    })
project_id, count = run_path(os.environ['TASKMASTER_DEMO_SEED'])['run'](env, db, logins=logins)
assert count == 24
env.cr.commit()
print(f'TASKMASTER_DEMO_PROJECT={project_id}', flush=True)
"""
    with TemporaryDirectory(prefix="taskmaster-demo-") as data_dir, ThreadingHTTPServer((_HOST, 0), _ModelDouble) as stub:
        child_env["TASKMASTER_MODEL_BASE_URL"] = f"http://{_HOST}:{stub.server_port}/v1"
        conf: Path = Path(data_dir) / "demo.conf"
        conf.write_text("[options]\n", encoding="ascii")
        common: list[str | Path] = [
            "-c", conf, "-r", args.pg_user, "--db_host", _HOST, "--db_port", str(args.pg_port), "-d", database,
            "--addons-path", f"{args.odoo.resolve() / 'addons'},{repo.parent}", "--data-dir", data_dir,
            "--max-cron-threads", "0", "--log-level", "warn",
        ]
        odoo: list[str] = [sys.executable, "-B", "odoo-bin"]
        pg: Popen = Popen([postgres, "-D", args.pg_data, "-h", _HOST, "-p", str(args.pg_port)],
                          stdout=DEVNULL, stderr=DEVNULL, creationflags=_CHILD_FLAGS)
        try:
            _wait_cluster(args.pg_bin, args.pg_port, process=pg)
            run([*odoo, *common, "-i", args.module, "--without-demo", "--no-http", "--stop-after-init"],
                cwd=args.odoo, env=child_env, check=True)
            result = run([*odoo, "shell", *common, "--no-http", "--shell-interface", "python"], cwd=args.odoo,
                         env=child_env, input=seed, text=True, encoding="utf-8", stdout=PIPE, check=True)
            project: int = int(next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                                    if line.startswith("TASKMASTER_DEMO_PROJECT=")))
            child_env.pop("TASKMASTER_DEMO_PASSWORD")
            thread: Thread = Thread(target=stub.serve_forever, daemon=True)
            thread.start()
            try:
                with Popen([*odoo, *common, "--db-filter", f"^{database}$", "--no-database-list",
                            "--http-interface", _HOST, "--http-port", str(args.http_port)],
                           cwd=args.odoo, env=child_env, creationflags=_CHILD_FLAGS) as server:
                    try:
                        _wait(args.http_port, process=server)
                        url: str = f"http://{_HOST}:{args.http_port}/taskmaster/risk-summary?project_id={project}"
                        print(f"\nURL: {url}\nLogin: {_LOGIN}\nPassword: {password}\n"
                              f"Synthetic database: {database}\nStop with Ctrl+C.\n", flush=True)
                        if not args.no_browser:
                            webbrowser.open(url)
                        server.wait()
                    finally:
                        if server.poll() is None:
                            server.terminate()
                            server.wait(timeout=15)
            finally:
                stub.shutdown()
                thread.join()
        finally:
            # Cleanup must never mask the original failure or leave the cluster running.
            try:
                run([dropdb, "--if-exists", "-h", _HOST, "-p", str(args.pg_port), "-U", args.pg_user, database],
                    stdout=DEVNULL, stderr=DEVNULL, timeout=30, check=False)
            except Exception:  # noqa: BLE001 -- report the original failure, not cleanup noise
                pass
            try:
                run([pg_ctl, "stop", "-D", args.pg_data, "-m", "fast", "-w"],
                    stdout=DEVNULL, stderr=DEVNULL, timeout=60, check=False)
            except Exception:  # noqa: BLE001 -- report the original failure, not cleanup noise
                pass
            if pg.poll() is None:
                pg.kill()
            pg.wait(timeout=15)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
