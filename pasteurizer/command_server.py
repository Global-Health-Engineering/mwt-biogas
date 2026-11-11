"""Operator command intake for the controller process (localhost only).

    POST /command   {"user": ..., "role": ..., "action": ..., "args": {...}}
                    header X-Command-Token: <token from the token file>
    GET  /health

Only the GUI server talks to this: it authenticates the person and passes on
the user name and role. The token file is written at start (owner read/write
only) and removed at stop, so no other program can post without reading it.

Commands never run in the HTTP thread. They are queued and executed by the
scan loop between cycles (`execute_pending`), through OperatorCommands, which
checks the role and the selector and logs every command with its outcome.
The HTTP request waits for that result (at most `wait_s`).
"""

from dataclasses import dataclass, field
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import queue
import secrets
import threading

from pasteurizer.operator_interface import CommandResult, Role, User
from pasteurizer.publisher import LOCALHOST, QuietServer, bind_error_category

ACTIONS = ("manual_request", "manual_release", "manual_clear", "level_override",
           "cancel_override", "allow_valve_test", "start", "stop", "reset", "acknowledge")
MAX_BODY = 4096


@dataclass
class _Job:
    user: User
    action: str
    args: dict
    done: threading.Event = field(default_factory=threading.Event)
    result: CommandResult | None = None


def execute(commands, job: _Job) -> CommandResult:
    """Run one command through OperatorCommands (scan thread only)."""
    a, user = job.args, job.user
    if job.action == "manual_request":
        return commands.manual_request(user, a.get("command"), a.get("value"),
                                       renew=a.get("renew") is True)
    if job.action == "manual_release":
        return commands.manual_release(user, a.get("command"))
    if job.action == "manual_clear":
        cause = a.get("cause")
        cause = cause[:40] if isinstance(cause, str) else None
        return commands.manual_clear(user, own=a.get("own") is True, cause=cause)
    if job.action == "level_override":
        return commands.level_override(user, a.get("switch"), a.get("value"),
                                       renew=a.get("renew") is True)
    if job.action == "cancel_override":
        return commands.cancel_override(user, a.get("switch"))
    if job.action == "allow_valve_test":
        return commands.allow_valve_test(user, renew=a.get("renew") is True)
    if job.action == "start":
        return commands.start(user)
    if job.action == "stop":
        return commands.stop(user)
    if job.action == "reset":
        return commands.reset(user)
    if job.action == "acknowledge":
        return commands.acknowledge(user, a.get("code"))
    return CommandResult(False, f"Unknown action {job.action!r}")


class CommandServer:
    def __init__(self, *, token_path, host="127.0.0.1", port=8082, wait_s=3.0):
        if host not in LOCALHOST:
            raise ValueError("The command server binds to localhost only")
        self.host, self.port = host, port
        self.token_path = Path(token_path)
        self.wait_s = wait_s
        self.token = secrets.token_urlsafe(32)
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=50)
        self._server = None
        self.error = self.error_category = None

    # Scan thread ------------------------------------------------------------
    def execute_pending(self, ctl, limit=10) -> int:
        """Run queued commands; call between scan cycles."""
        count = 0
        while count < limit:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                job.result = execute(ctl.commands, job)
            except Exception as error:          # a command can never stop the scan
                job.result = CommandResult(False, f"Command failed: {type(error).__name__}: {error}")
            job.done.set()
            count += 1
        return count

    # HTTP -------------------------------------------------------------------
    def start(self) -> bool:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "Not found"})

            def do_POST(self):
                if self.path != "/command":
                    return self._json(404, {"error": "Not found"})
                supplied = self.headers.get("X-Command-Token", "")
                if not hmac.compare_digest(supplied.encode(), server.token.encode()):
                    return self._json(403, {"error": "Invalid command token"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError("Body missing or too large")
                    body = json.loads(self.rfile.read(length))
                    action = body["action"]
                    if action not in ACTIONS:
                        raise ValueError(f"Unknown action {action!r}")
                    user = User(str(body["user"]), Role(body["role"]))
                    args = body.get("args") or {}
                    if not isinstance(args, dict):
                        raise ValueError("args must be an object")
                except (ValueError, KeyError, TypeError) as error:
                    return self._json(400, {"error": str(error)})
                job = _Job(user, action, args)
                try:
                    server._queue.put_nowait(job)
                except queue.Full:
                    return self._json(503, {"error": "Controller busy"})
                if not job.done.wait(server.wait_s):
                    return self._json(504, {"error": "Controller did not process the command"})
                return self._json(200, {"accepted": job.result.accepted,
                                        "reason": job.result.reason})

            def _refuse(self):
                self._json(405, {"error": "Method not allowed"})

            do_PUT = do_DELETE = do_PATCH = _refuse

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        try:
            self._server = QuietServer((self.host, self.port), Handler)
        except OSError as error:
            self.error = f"{type(error).__name__}: {error}"
            self.error_category = bind_error_category(error)
            return False
        self.port = self._server.server_address[1]
        self._write_token()
        threading.Thread(target=self._server.serve_forever, name="command-server",
                         daemon=True).start()
        return True

    def _write_token(self):
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        if self.token_path.exists():
            self.token_path.unlink()
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(f"{self.port}\n{self.token}\n")

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            self.token_path.unlink()
        except FileNotFoundError:
            pass


def read_token_file(path) -> tuple[int, str] | None:
    """(port, token) of the running controller, or None if it is not running."""
    try:
        port, token = Path(path).read_text(encoding="utf-8").split()
        return int(port), token
    except (OSError, ValueError):
        return None
