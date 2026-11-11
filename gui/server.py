"""HTTP server of the GUI (stdlib only).

    /                 main window (EDiBl, Pasteurizer, Trends)
    /log              log window (opened separately; same server, same data)
    /static/<file>    page assets
    /api/live         latest controller snapshot + connection state
    /api/meta         tag map, legend, controller config flags
    /api/events       history events (filters, paging, live follow)
    /api/trend        trend series (decimated, gaps, runs)
    /api/runs         controller runs
    /api/export.csv   filtered events as CSV
    /api/session      who is logged in (GET)
    POST /api/login   {"name", "password"} -> session cookie
    POST /api/logout
    POST /api/command {"action", "args"}: logged-in users only; passed to the
                      controller's command server with its token, the user
                      name and the role from the users file

Without a users file nobody can log in and the GUI is read-only. Every POST
needs the header X-Requested-With: mwt-gui (a page from another site cannot
set it) and the session cookie is SameSite=Strict and HttpOnly. The
controller checks role and selector again and logs every command.
"""

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
import json
import mimetypes
from pathlib import Path
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlparse
import urllib.request

from gui import history_api
from gui.auth import SessionStore, UserStore
from gui.tags import EQUIPMENT, LEGEND
from pasteurizer.command_server import read_token_file
from pasteurizer.publisher import QuietServer

STATIC = Path(__file__).resolve().parent / "static"
PAGES = {"/": "index.html", "/log": "log.html"}
COOKIE = "mwt_session"
CSRF_HEADER = ("X-Requested-With", "mwt-gui")
MAX_BODY = 4096
FAILED_LOGIN_DELAY_S = 1.0


class GuiServer:
    def __init__(self, link, database, *, host="127.0.0.1", port=8080,
                 users=None, command_token=None):
        """users: path of the users file (None: no logins, read-only).
        command_token: path of the controller's command token file."""
        self.link = link
        self.database = Path(database)
        self.host, self.port = host, port
        self.users = UserStore(users) if users is not None else None
        self.command_token = Path(command_token) if command_token is not None else None
        self.sessions = SessionStore(on_end=self._session_ended)
        self.session_clears = []        # (user, cause, HTTP status, result): last few, for status/tests
        self._server = None
        self._stop = threading.Event()

    def _session_ended(self, session, cause):
        """Logout, expiry or GUI stop: withdraw that user's manual requests,
        overrides and valve-test allowance in the controller at once. If this
        message is lost the controller still expires them (30 s / 10 s)."""
        if session.role != "COMMISSIONING":
            return                      # only commissioning users can hold requests
        code, result = self.forward(session, "manual_clear", {"own": True, "cause": cause})
        self.session_clears = (self.session_clears + [(session.user, cause, code, result)])[-20:]

    def forward(self, session, action, args) -> tuple[int, dict]:
        """Pass a command to the controller; (HTTP status, body)."""
        found = read_token_file(self.command_token) if self.command_token else None
        if found is None:
            return 503, {"error": "Controller command channel not available "
                                  "(controller stopped or started without commands)"}
        port, token = found
        body = json.dumps({"user": session.user, "role": session.role,
                           "action": action, "args": args}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/command", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-Command-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            except ValueError:
                return error.code, {"error": str(error)}
        except (URLError, OSError) as error:
            return 503, {"error": f"Controller not reachable: {error}"}

    def handler(self):
        gui = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                params = dict(parse_qsl(url.query))
                try:
                    if url.path in PAGES:
                        return self._file(STATIC / PAGES[url.path])
                    if url.path.startswith("/static/"):
                        name = url.path[len("/static/"):]
                        target = (STATIC / name).resolve()
                        if STATIC not in target.parents or not target.is_file():
                            return self._json(404, {"error": "Not found"})
                        return self._file(target)
                    if url.path == "/api/session":
                        session = self._session()
                        return self._json(200, {
                            "user": session.user if session else None,
                            "role": session.role if session else None,
                            "logins": gui.users is not None,
                            "commands": read_token_file(gui.command_token) is not None
                            if gui.command_token else False})
                    if url.path == "/api/live":
                        view = gui.link.view()
                        view["gui_time"] = time.time()
                        return self._json(200, view)
                    if url.path == "/api/meta":
                        return self._json(200, {"equipment": EQUIPMENT, "legend": LEGEND,
                                                "config": gui.link.view()["config"]})
                    if url.path in ("/api/events", "/api/trend", "/api/runs",
                                    "/api/export.csv") and not gui.database.exists():
                        return self._json(503, {"error": "No historian database yet"})
                    if url.path == "/api/events":
                        return self._json(200, history_api.events(gui.database, params))
                    if url.path == "/api/trend":
                        return self._json(200, history_api.trend(gui.database, params))
                    if url.path == "/api/runs":
                        return self._json(200, history_api.runs(gui.database))
                    if url.path == "/api/export.csv":
                        return self._csv(history_api.export_events(gui.database, params))
                    return self._json(404, {"error": "Not found"})
                except (ValueError, KeyError) as error:
                    return self._json(400, {"error": str(error)})
                except sqlite3.Error as error:
                    return self._json(503, {"error": f"History unavailable: {error}"})

            def do_POST(self):
                url = urlparse(self.path)
                if url.path not in ("/api/login", "/api/logout", "/api/command"):
                    return self._json(405, {"error": "Read-only"})
                if self.headers.get(CSRF_HEADER[0]) != CSRF_HEADER[1]:
                    return self._json(403, {"error": "Missing request header"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= MAX_BODY:
                        raise ValueError("Body too large")
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("Body must be an object")
                except ValueError as error:
                    return self._json(400, {"error": str(error)})
                if url.path == "/api/login":
                    return self._login(body)
                if url.path == "/api/logout":
                    gui.sessions.end(self._cookie(), "LOGOUT")   # withdraws manual requests
                    return self._json(200, {"ok": True}, cookie="")
                session = self._session()
                if session is None:
                    return self._json(401, {"error": "Log in first"})
                args = body.get("args") or {}
                if not isinstance(args, dict):
                    return self._json(400, {"error": "args must be an object"})
                code, result = gui.forward(session, str(body.get("action")), args)
                return self._json(code, result)

            def _login(self, body):
                if gui.users is None:
                    return self._json(403, {"error": "Logins are not configured on this GUI"})
                name, password = str(body.get("name", "")), str(body.get("password", ""))
                wait = gui.sessions.locked_out(name)
                if wait:
                    return self._json(429, {"error": f"Too many attempts; wait {wait:.0f} s"})
                role = gui.users.verify(name, password)
                if role is None:
                    gui.sessions.failed(name)
                    time.sleep(FAILED_LOGIN_DELAY_S)
                    return self._json(401, {"error": "Wrong name or password"})
                token = gui.sessions.create(name, role)
                return self._json(200, {"user": name, "role": role}, cookie=token)

            def _cookie(self):
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                return cookie[COOKIE].value if COOKIE in cookie else None

            def _session(self):
                return gui.sessions.get(self._cookie())

            def _refuse(self):
                self._json(405, {"error": "Method not allowed"})

            do_PUT = do_DELETE = do_PATCH = _refuse

            def _json(self, code, body, cookie=None):
                data = json.dumps(body, allow_nan=False, default=str).encode()
                extra = []
                if cookie is not None:      # "" clears the cookie
                    extra.append(("Set-Cookie", f"{COOKIE}={cookie}; Path=/; HttpOnly; "
                                  f"SameSite=Strict{'' if cookie else '; Max-Age=0'}"))
                self._send(code, "application/json", data, extra)

            def _file(self, path):
                kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._send(200, kind, path.read_bytes())

            def _csv(self, chunks):
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="mwt-biogas-events.csv"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(chunk.encode())

            def _send(self, code, kind, data, extra=()):
                self.send_response(code)
                for name, value in extra:
                    self.send_header(name, value)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        return Handler

    def start(self):
        self._server = QuietServer((self.host, self.port), self.handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="gui-http",
                         daemon=True).start()
        threading.Thread(target=self._sweep, name="gui-sessions", daemon=True).start()

    def _sweep(self, every_s=5.0):
        """Expired sessions end within a few seconds even if no request comes."""
        while not self._stop.wait(every_s):
            self.sessions.sweep()

    def stop(self):
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self.sessions.end_all("GUI_STOPPED")
