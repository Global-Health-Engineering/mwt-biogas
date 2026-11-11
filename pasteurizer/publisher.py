"""Read-only status publisher for the GUI (localhost only).

The scan calls `publish(status)`, which only swaps a reference: it never
waits for a client and never fails the scan. A daemon thread serves

    GET /status   latest ControllerStatus as JSON (503 before the first)
    GET /config   provisional / unvalidated flags and the config hash
    GET /health   {"ok": true}

on 127.0.0.1 only. Every other method or path is refused; nothing here can
change the controller, its outputs or its settings.
"""

from dataclasses import asdict
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import sys
import threading

from pasteurizer.operator_interface import MANUAL_COMMANDS

LOCALHOST = ("127.0.0.1", "localhost", "::1")


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def config_info(config) -> dict:
    """The provisional and unvalidated flags the GUI shows in equipment details."""
    control = config.control
    return {
        "config_hash": config.config_hash,
        "settings_validated": control.settings_validated,
        "model_control_enabled": control.model_control_enabled,
        "temperature_ranges_validated": config.temperature_ranges_validated,
        "physical_assignments_verified": bool(
            config.roles.get("physical_assignments_verified", False)),
        "flow_calibration_confirmed": {
            name: bool(getattr(flow, "calibration_confirmed", False))
            for name, flow in config.flows.items()},
        "model_validated": bool(config.treatment.get("model_validated", False)),
        "startup_thresholds_validated": bool(
            getattr(config.startup, "thresholds_validated", False)),
        # For the manual panel: which RevPi channel each command drives.
        "output_channels": _output_channels(config),
        "pending_identification": sorted(
            config.io_mapping.get("verification", {})
            .get("valve_outputs_pending_identification", {}).values()),
        "manual_commands": list(MANUAL_COMMANDS),
        "request_expiry_s": control.manual.request_expiry_s,
        "override_expiry_s": control.manual.override_expiry_s,
        "level_roles": dict(config.level_roles),
    }


def _output_channels(config) -> dict:
    digital = config.io_mapping.get("digital_outputs", {})
    channels = {command: digital.get(equipment, equipment)
                for command, equipment in config.command_outputs.items()}
    y4 = config.io_mapping.get("control_valve_Y4", {}).get("command")
    if y4:
        channels["control_valve_percent"] = y4
    return channels


# Bind failures by error code, never by message text (Windows messages differ
# and are localised). On Windows a socket error carries the Winsock code
# (10048, 10013); errno.EADDRINUSE there is the C runtime value 100.
ADDRESS_IN_USE = "ADDRESS_IN_USE"
ACCESS_DENIED = "ACCESS_DENIED"
BIND_FAILED = "BIND_FAILED"
_IN_USE_CODES = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048), 10048}
_DENIED_CODES = {errno.EACCES, getattr(errno, "WSAEACCES", 10013), 10013}


def bind_error_category(error: OSError) -> str:
    codes = {getattr(error, "errno", None), getattr(error, "winerror", None)}
    if codes & _IN_USE_CODES:
        return ADDRESS_IN_USE
    if codes & _DENIED_CODES:          # Windows also reports a port held exclusively this way
        return ACCESS_DENIED
    return BIND_FAILED


class QuietServer(ThreadingHTTPServer):
    """Local HTTP server: one listener per address and port, quiet on client hang-ups.

    http.server sets SO_REUSEADDR for every server. On Linux and macOS that
    only allows rebinding while old connections are in TIME_WAIT; a second
    listener on the same port is still refused. On Windows SO_REUSEADDR lets
    a second socket bind a port that is already listening, so a second
    controller or GUI would start silently. It is therefore off on Windows,
    where the default already refuses a second bind (WSAEADDRINUSE).
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class StatusPublisher:
    def __init__(self, *, host="127.0.0.1", port=8081, config=None):
        if host not in LOCALHOST:
            raise ValueError("The status publisher binds to localhost only")
        self.host, self.port = host, port
        self._config = config_info(config) if config is not None else {}
        self._latest = None
        self._server = None
        self._thread = None
        self.error = None                   # text for people
        self.error_category = None          # ADDRESS_IN_USE / ACCESS_DENIED / BIND_FAILED
        self.error_code = None              # errno or Winsock code

    def publish(self, status):
        """Called by the scan: a reference swap, never blocking."""
        self._latest = status

    def start(self) -> bool:
        publisher = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/status":
                    status = publisher._latest
                    if status is None:
                        return self._json(503, {"error": "No snapshot yet"})
                    return self._json(200, _clean(asdict(status)))
                if self.path == "/config":
                    return self._json(200, publisher._config)
                if self.path == "/health":
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "Not found"})

            def _refuse(self):
                self._json(405, {"error": "Read-only"})

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

            def _json(self, code, body):
                data = json.dumps(body, allow_nan=False, default=str).encode()
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
        except OSError as error:            # port in use: the controller runs on
            self.error = f"{type(error).__name__}: {error}"
            self.error_category = bind_error_category(error)
            self.error_code = getattr(error, "winerror", None) or error.errno
            return False
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="status-publisher", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
