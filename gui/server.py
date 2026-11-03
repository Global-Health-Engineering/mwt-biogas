"""HTTP server of the read-only GUI (stdlib only; GET requests only).

    /                 main window (EDiBl, Pasteurizer, Trends)
    /log              log window (opened separately; same server, same data)
    /static/<file>    page assets
    /api/live         latest controller snapshot + connection state
    /api/meta         tag map, legend, controller config flags
    /api/events       history events (filters, paging, live follow)
    /api/trend        trend series (decimated, gaps, runs)
    /api/runs         controller runs
    /api/export.csv   filtered events as CSV

Opening more windows only opens more pages on this server; the GUI never
starts a controller and has no plant commands.
"""

from http.server import BaseHTTPRequestHandler
import json
import mimetypes
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlparse

from gui import history_api
from gui.tags import EQUIPMENT, LEGEND
from pasteurizer.publisher import QuietServer

STATIC = Path(__file__).resolve().parent / "static"
PAGES = {"/": "index.html", "/log": "log.html"}


class GuiServer:
    def __init__(self, link, database, *, host="127.0.0.1", port=8080):
        self.link = link
        self.database = Path(database)
        self.host, self.port = host, port
        self._server = None

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

            def _refuse(self):
                self._json(405, {"error": "Read-only GUI"})

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

            def _json(self, code, body):
                data = json.dumps(body, allow_nan=False, default=str).encode()
                self._send(code, "application/json", data)

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

            def _send(self, code, kind, data):
                self.send_response(code)
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

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
