"""Session-only monitor guardian. No service, autostart or power action is installed.

The guardian only starts the fixed Elipro module and waits for that exact child
to exit. A normal stop is final; an unexpected process exit permits a bounded
retry after USB cooldown. Every monitor child still acquires the global monitor
ownership lock before accessing the UPS.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import argparse
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .instance_guard import GuardianInstanceGuard, InstanceGuardError
from .runtime import child_command as runtime_command, default_data_dir, resource_root


ROOT = resource_root()
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 300
MIN_RETRY_SECONDS = 5


def _utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "No se permiten redirecciones locales", headers, fp)


def _opener():
    return build_opener(ProxyHandler({}), _NoRedirect())


def fetch_status(port):
    request = Request(f"http://127.0.0.1:{port}/api/status", headers={"Accept": "application/json"})
    with _opener().open(request, timeout=2) as response:
        payload = response.read(262145)
        if len(payload) > 262144:
            raise ValueError("Estado local demasiado grande")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Estado local inválido")
    return value


def send_stop(port, token):
    request = Request(f"http://127.0.0.1:{port}/api/service/stop", data=b"{}", method="POST",
                      headers={"Content-Type": "application/json", "X-UPS-Token": token})
    with _opener().open(request, timeout=2) as response:
        payload = response.read(4097)
        if len(payload) > 4096:
            return False
    value = json.loads(payload)
    return isinstance(value, dict) and value.get("stopped") is True


def write_state(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def request_parent_stop(data_dir, port=None):
    """Ask only this monitor's verified guardian session not to restart it."""
    try:
        parent = os.environ.get("ELIPRO_GUARDIAN_PID", "")
        session = os.environ.get("ELIPRO_GUARDIAN_SESSION", "")
        if not parent.isdecimal() or int(parent) <= 0 or len(session) != 32:
            return False
        directory = Path(data_dir)
        with (directory / "guardian.json").open("r", encoding="utf-8") as file:
            payload = file.read(4097)
        if len(payload) > 4096:
            return False
        state = json.loads(payload)
        if (not isinstance(state, dict) or type(state.get("child_pid")) is not int
                or state["child_pid"] != os.getpid() or type(state.get("pid")) is not int
                or state["pid"] != int(parent) or state.get("session_id") != session
                or port is not None and state.get("port") != port):
            return False
        marker = {"guardian_pid": int(parent), "session_id": session}
        temporary = directory / f".guardian.stop.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(marker, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(directory / "guardian.stop")
        return True
    except (OSError, ValueError, TypeError):
        return False


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Supervisión de Elipro durante esta sesión de Windows")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--source", choices=("auto", "native", "bridge"), default="auto")
    parser.add_argument("--forza-url", default="http://127.0.0.1:15179/Forza")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port debe estar entre 1 y 65535")
    try:
        url = urlsplit(args.forza_url)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"}
                or url.username is not None or url.password is not None or url.query or url.fragment
                or not 1 <= (url.port or 80) <= 65535):
            raise ValueError("URL de Forza inválida")
        args.data_dir = args.data_dir.expanduser().resolve()
    except (OSError, ValueError):
        parser.error("La carpeta o la dirección local de Forza no es válida")
    return args


def child_command(options):
    command = runtime_command("monitor", "--port", str(options.port),
               "--source", options.source, "--forza-url", options.forza_url,
               "--data-dir", str(options.data_dir))
    if options.dry_run:
        command.append("--dry-run")
    return command


class Supervisor:
    def __init__(self, options, *, process_factory=None, clock=None, sleep=None,
                 status_fetcher=None, stop_sender=None, state_writer=None, ownership_factory=None):
        self.options = options
        self.process_factory = process_factory or subprocess.Popen
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.status_fetcher = status_fetcher or fetch_status
        self.stop_sender = stop_sender or send_stop
        self.state_writer = state_writer or write_state
        self.ownership_factory = ownership_factory or GuardianInstanceGuard
        self.child = None
        self.stop_requested = threading.Event()
        self.stop_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.started_at = _utc()
        self.state_path = Path(options.data_dir) / "guardian.json"
        self.stop_path = Path(options.data_dir) / "guardian.stop"
        self.session_id = secrets.token_hex(16)
        self.state = {"pid": os.getpid(), "child_pid": None, "port": options.port,
                      "session_id": self.session_id, "state": "starting",
                      "started_at": self.started_at, "heartbeat_at": self.started_at,
                      "restarts": 0, "last_exit_code": None, "next_retry_seconds": None}
        self.failures = deque()
        self.consecutive_failures = 0
        self.last_heartbeat = float("-inf")

    def _publish(self, state=None, **updates):
        with self.state_lock:
            if state is not None:
                self.state["state"] = state
            self.state.update(updates)
            self.state["heartbeat_at"] = _utc()
            value = dict(self.state)
            self.last_heartbeat = self.clock()
        try:
            self.state_writer(self.state_path, value)
        except Exception:
            # The monitor has its own critical loop and history error handling.
            # A failing guardian status file never stops or replaces its child.
            pass

    def _wait_child(self):
        while True:
            code = self.child.poll()
            if code is not None:
                return code
            if self.stop_requested.is_set():
                return None
            if self._marker_requested():
                self.stop()
                return self.child.poll()
            if self.clock() - self.last_heartbeat >= 5:
                self._publish("running", child_pid=self.child.pid)
            try:
                return self.child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue

    def _retry(self, seconds):
        remaining = seconds
        while remaining > 0 and not self.stop_requested.is_set():
            if self._marker_requested():
                self.stop_requested.set()
                break
            self._publish("retrying", child_pid=None, next_retry_seconds=remaining)
            delay = min(0.2, remaining)
            self.sleep(delay)
            remaining = max(0, round(remaining - delay, 10))

    def _marker_requested(self):
        try:
            with self.stop_path.open("r", encoding="utf-8") as file:
                payload = file.read(4097)
            if len(payload) > 4096:
                return False
            value = json.loads(payload)
            return (isinstance(value, dict) and type(value.get("guardian_pid")) is int
                    and value["guardian_pid"] == os.getpid() and value.get("session_id") == self.session_id)
        except (OSError, ValueError, TypeError):
            return False

    def _stop_child(self):
        child = self.child
        if child is None:
            return True
        budget, requested = 15.0, False
        while budget > 0:
            started = self.clock()
            try:
                if child.poll() is not None:
                    return True
            except OSError:
                return False
            if not requested:
                try:
                    status = self.status_fetcher(self.options.port)
                    health = status.get("health", {})
                    process = health.get("process", {}) if isinstance(health, dict) else {}
                    meta = status.get("meta", {})
                    token = status.get("csrf_token")
                    if (isinstance(meta, dict) and meta.get("name") == "Elipro UPS"
                            and isinstance(process, dict) and type(process.get("pid")) is int
                            and process["pid"] == child.pid and isinstance(token, str) and 16 <= len(token) <= 256):
                        requested = self.stop_sender(self.options.port, token) is True
                except Exception:
                    pass
            remaining = budget - max(0, self.clock() - started)
            if remaining <= 0:
                break
            # A child may still be starting its HTTP server when Stop arrives.
            # Retry identity until it is ready, without ever targeting a peer.
            wait_seconds = remaining if requested else min(0.2, remaining)
            try:
                child.wait(timeout=wait_seconds)
                return child.poll() is not None
            except subprocess.TimeoutExpired:
                # Account for injected fake waits too, so tests and unusual
                # clocks cannot turn an unavailable API into an endless loop.
                budget = max(0, budget - max(wait_seconds, self.clock() - started))
            except OSError:
                return False
        return False

    def stop(self):
        self.stop_requested.set()
        with self.stop_lock:
            self._publish("stopping", next_retry_seconds=None)
            stopped = self._stop_child()
            self._publish("stopped" if stopped else "failed", child_pid=None if stopped else self.child.pid,
                          last_exit_code=self.child.poll() if self.child is not None else None)
            return stopped

    def run(self):
        ownership = self.ownership_factory()
        try:
            ownership.acquire()
        except InstanceGuardError as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 2
        try:
            return self._run_owned()
        finally:
            # An uncertain live child remains the only reader. Keep guardian
            # ownership reserved until process exit instead of admitting peers.
            try:
                child_exited = self.child is None or self.child.poll() is not None
            except OSError:
                child_exited = False
            if child_exited:
                ownership.close()

    def _run_owned(self):
        self._publish("starting")
        try:
            while not self.stop_requested.is_set():
                if self._marker_requested():
                    self.stop_requested.set()
                    break
                started = self.clock()
                try:
                    environment = os.environ.copy()
                    environment["ELIPRO_GUARDIAN_PID"] = str(os.getpid())
                    environment["ELIPRO_GUARDIAN_SESSION"] = self.session_id
                    environment["ELIPRO_GUARDIAN_RESTARTS"] = str(self.state["restarts"])
                    self.child = self.process_factory(child_command(self.options), env=environment,
                                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=str(ROOT))
                except OSError:
                    self.child = None
                    code = None
                else:
                    self._publish("running", child_pid=self.child.pid, next_retry_seconds=None)
                    try:
                        code = self._wait_child()
                    except OSError:
                        # Losing the process handle is not proof of exit.
                        self._publish("failed", next_retry_seconds=None)
                        return 3
                if self.stop_requested.is_set():
                    if self.child is not None and self.child.poll() is None:
                        return 3
                    self._publish("stopped", child_pid=None, next_retry_seconds=None)
                    return 0
                self._publish(last_exit_code=code, child_pid=None)
                if code in (0, 2):
                    self._publish("stopped", next_retry_seconds=None)
                    return code
                now = self.clock()
                if now - started >= FAILURE_WINDOW_SECONDS:
                    self.consecutive_failures = 0
                while self.failures and now - self.failures[0] >= FAILURE_WINDOW_SECONDS:
                    self.failures.popleft()
                self.failures.append(now)
                if len(self.failures) >= MAX_FAILURES:
                    self._publish("failed", next_retry_seconds=None)
                    return 3
                retry = min(30, MIN_RETRY_SECONDS * 2 ** min(self.consecutive_failures, 3))
                self.consecutive_failures += 1
                self._retry(retry)
                if self.stop_requested.is_set():
                    break
                with self.state_lock:
                    self.state["restarts"] += 1
            self._publish("stopped", child_pid=None, next_retry_seconds=None)
            return 0
        except KeyboardInterrupt:
            return 0 if self.stop() else 3


def main(argv=None):
    options = parse_args(argv)
    raise SystemExit(Supervisor(options).run())


if __name__ == "__main__":
    main()
