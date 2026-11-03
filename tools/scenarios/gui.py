"""Scenarios for the read-only GUI: status publisher, controller link, GUI server.

Everything runs on localhost with OS-chosen ports. The controller is the usual
simulated Sim; nothing here touches RevPi I/O.
"""

import csv
import errno
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from gui.link import ControllerLink
from gui.server import GuiServer
from pasteurizer.publisher import (
    ADDRESS_IN_USE, ACCESS_DENIED, BIND_FAILED, QuietServer, StatusPublisher, bind_error_category,
)
from scenarios.harness import FAST, OPERATOR, Sim, scenario, to_fallback_treatment
from scenarios.historian import temp_db

ROOT = Path(__file__).resolve().parents[2]
IN_USE_CODES = {errno.EADDRINUSE, 10048}      # POSIX errno, Winsock WSAEADDRINUSE
OMITTED_MANUAL = ("EDB-VL-01", "EDB-VL-02", "EDB-VL-07", "EDB-VL-08", "EDB-VL-11", "EDB-VL-12")


def _strict(text):
    def refuse(name):
        raise AssertionError(f"non-JSON constant {name}")
    return json.loads(text, parse_constant=refuse)


def request(port, path, method="GET", raw=False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
    finally:
        connection.close()
    if raw:
        return response.status, body, headers
    return response.status, (_strict(body) if body else None)


class Rig:
    """Sim + status publisher + link (manual polling on a fake clock) + GUI server."""

    def __init__(self, database=None, **sim_args):
        self.sim = Sim(params=FAST, bath=84.0, database=database, **sim_args)
        self.publisher = StatusPublisher(port=0, config=self.sim.config)
        assert self.publisher.start()
        self.sim.ctl.publish = self.publisher.publish
        self.t = 0.0
        self.link = ControllerLink(f"http://127.0.0.1:{self.publisher.port}",
                                   clock=lambda: self.t)
        self.gui = GuiServer(self.link, database or temp_db(), port=0)
        self.gui.start()

    def poll(self, advance=0.0):
        self.t += advance
        self.link.poll_once()
        return self.link.view()

    def close(self):
        self.gui.stop()
        self.publisher.stop()


@scenario(74, "GUI link: connected, stale after 2 s while answering, disconnected, reconnected")
def s74():
    rig = Rig()
    try:
        assert rig.link.view()["state"] == "WAITING"
        rig.sim.run(1)
        view = rig.poll()
        assert view["state"] == "CONNECTED" and view["responding"]
        assert view["status"]["cycle"] == rig.sim.status.cycle
        assert view["config"]["config_hash"] == rig.sim.config.config_hash
        first_boot = view["status"]["boot_id"]
        # The publisher answers, but the snapshot does not change (scan stopped).
        assert rig.poll(1.0)["state"] == "CONNECTED"
        view = rig.poll(1.5)
        assert view["state"] == "STALE" and view["responding"] and view["age_s"] >= 2.0
        assert view["status"] is not None                 # last values kept, shown greyed
        rig.sim.run(0.2)
        assert rig.poll(0.5)["state"] == "CONNECTED"
        # Publisher gone: not responding, DISCONNECTED once 2 s pass without data.
        port = rig.publisher.port
        rig.publisher.stop()
        view = rig.poll(1.0)
        assert not view["responding"] and view["error"] and view["state"] == "CONNECTED"
        assert rig.poll(1.5)["state"] == "DISCONNECTED"
        # Controller restarts on the same port: new boot, config fetched again.
        sim2 = rig.sim.restart(params=FAST)
        rig.publisher = StatusPublisher(port=port, config=sim2.config)
        assert rig.publisher.start()
        sim2.ctl.publish = rig.publisher.publish
        sim2.run(0.4)
        view = rig.poll(1.0)
        assert view["state"] == "CONNECTED" and view["status"]["boot_id"] != first_boot
        assert view["status"]["boot_id"] == sim2.log.boot_id
    finally:
        rig.close()


@scenario(75, "Status publisher: localhost only, read-only, strict JSON, never stops the scan")
def s75():
    try:
        StatusPublisher(host="0.0.0.0", port=0)
        raise AssertionError("publisher accepted a non-local address")
    except ValueError:
        pass
    sim = Sim(params=FAST, bath=84.0)
    publisher = StatusPublisher(port=0, config=sim.config)
    second = stuck = None
    assert publisher.start(), publisher.error
    try:
        assert request(publisher.port, "/status")[0] == 503          # no snapshot yet
        assert request(publisher.port, "/health") == (200, {"ok": True})
        sim.ctl.publish = publisher.publish
        sim.plant.faults.invalid_temperatures.add("PT3_B4")
        sim.plant.faults.y4_feedback_invalid = True
        sim.run(2)
        code, status = request(publisher.port, "/status")               # strict JSON parse
        assert code == 200 and status["cycle"] == sim.status.cycle
        pt3 = status["signals"]["temperatures"]["PT3_B4"]
        assert pt3["valid"] is False and pt3["celsius"] is None
        assert status["signals"]["valve"]["feedback"]["valid"] is False
        info = request(publisher.port, "/config")[1]
        assert info["config_hash"] == sim.config.config_hash
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert request(publisher.port, "/status", method)[0] == 405
        # Port taken: the second publisher reports it, nothing raises.
        second = StatusPublisher(port=publisher.port)
        _assert_refused_second_bind(second, publisher)
        # A client that never finishes its request does not hold up the scan.
        stuck = socket.create_connection(("127.0.0.1", publisher.port))
        stuck.sendall(b"GET /status HTTP/1.1\r\nHost: x\r\n")      # headers never end
        cycle = sim.status.cycle
        sim.run(2)
        assert sim.status.cycle == cycle + 10
        assert request(publisher.port, "/status")[1]["cycle"] == sim.status.cycle
        stuck.close()
        started = time.perf_counter()
        for _ in range(10000):
            publisher.publish(sim.status)
        assert time.perf_counter() - started < 0.05                   # a reference swap
    finally:
        if stuck is not None:
            stuck.close()
        if second is not None:
            second.stop()                 # a no-op unless it (wrongly) bound the port
        publisher.stop()
    # A publisher that fails cannot stop the scan.
    def broken(_status):
        raise RuntimeError("GUI path failure")
    sim.ctl.publish = broken
    cycle = sim.status.cycle
    sim.run(1)
    assert sim.status.cycle == cycle + 5


def _assert_refused_second_bind(second, first):
    """A second publisher on a port in use must not start; checked by error code."""
    started = second.start()
    assert started is False, (
        f"second publisher bound port {first.port} while the first listens: start() returned "
        f"{started!r}, error {second.error!r}, category {second.error_category!r}")
    assert second.error_category == ADDRESS_IN_USE, (
        f"expected {ADDRESS_IN_USE}, got category {second.error_category!r}, "
        f"code {second.error_code!r}, error {second.error!r}")
    assert second.error_code in IN_USE_CODES, second.error_code
    assert request(first.port, "/health") == (200, {"ok": True})     # the first still serves


@scenario(76, "GUI server: pages, live view, read-only, no path escape, no history without a database")
def s76():
    rig = Rig(database=temp_db())
    try:
        rig.sim.run(1)
        rig.poll()
        port = rig.gui.port
        for path in ("/", "/log", "/static/app.js", "/static/style.css"):
            assert request(port, path, raw=True)[0] == 200, path
        page = request(port, "/", raw=True)[1]
        assert "Read-only" in page and "<form" not in page          # nothing to submit
        for path in ("/static/../server.py", "/static/%2e%2e/server.py",
                     "/static/..%2fserver.py", "/static/../../pasteurizer/publisher.py"):
            assert request(port, path, raw=True)[0] == 404, path
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert request(port, "/api/live", method)[0] == 405
        code, live = request(port, "/api/live")
        assert code == 200 and live["state"] == "CONNECTED"
        assert live["status"]["data_source"] == "SIMULATED"
        meta = request(port, "/api/meta")[1]
        tags = {e["tag"] for e in meta["equipment"]}
        assert not tags & set(OMITTED_MANUAL)                         # group 7 decision
        manual = {e["tag"] for e in meta["equipment"] if e["kind"] == "manual"}
        assert manual == {"EDB-VL-03", "EDB-VL-04", "EDB-VL-09", "EDB-VL-10",
                          "EDB-VL-13", "GPST-VL-07"}
        assert rig.gui.host == "127.0.0.1"
        # The browser pages send no commands: no fetch with a method other than GET.
        for name in ("app.js", "log.js", "trends.js", "common.js"):
            text = (ROOT / "gui" / "static" / name).read_text(encoding="utf-8")
            assert "method:" not in text and "POST" not in text, name
    finally:
        rig.close()
    link = ControllerLink("http://127.0.0.1:9")          # nothing listens there
    gui = GuiServer(link, temp_db(), port=0)
    gui.start()
    try:
        assert request(gui.port, "/api/events")[0] == 503
        link.poll_once()
        assert request(gui.port, "/api/live")[1]["state"] == "DISCONNECTED"
    finally:
        gui.stop()


@scenario(77, "Active faults and invalid data reach the GUI as alarms, lamps and Unavailable values")
def s77():
    database = temp_db()
    rig = Rig(database=database)
    try:
        to_fallback_treatment(rig.sim)
        rig.poll()
        live = request(rig.gui.port, "/api/live")[1]
        assert live["status"]["lamp_modes"] == {"green": "steady", "yellow": "off",
                                                "red": "blinking"}
        rig.sim.plant.faults.temperature_offsets["PT6_B7"] = 8.0       # above the 90 C trip
        rig.sim.plant.faults.invalid_temperatures.add("PT3_B4")
        rig.sim.run(1)
        rig.poll(0.5)
        status = request(rig.gui.port, "/api/live")[1]["status"]
        alarms = {a["code"]: a for a in status["alarms"]}
        assert alarms["OVERTEMPERATURE"]["severity"] == "CRITICAL"
        assert alarms["OVERTEMPERATURE"]["equipment"] == ["PT5_B6", "PT6_B7"]
        assert alarms["OVERTEMPERATURE"]["raised_utc"]
        assert status["signals"]["temperatures"]["PT3_B4"]["celsius"] is None
        assert status["lamp_modes"]["red"] == "steady"
        rig.sim.run(2)
        events = request(rig.gui.port, "/api/events?severity=CRITICAL&limit=20")[1]["rows"]
        raised = [r for r in events if r["kind"] == "ALARM_RAISED"
                  and r["data"]["code"] == "OVERTEMPERATURE"]
        assert raised and raised[0]["procedure"] == "No documented procedure"
        assert set(raised[0]["equipment"]) == {"PT5_B6", "PT6_B7"}
        assert raised[0]["description"].startswith("OVERTEMPERATURE raised")
    finally:
        rig.close()


@scenario(78, "Log windows: paging, live follow, two windows at once, CSV export of the filter")
def s78():
    database = temp_db()
    rig = Rig(database=database)
    try:
        to_fallback_treatment(rig.sim)
        rig.sim.plant.faults.temperature_offsets["PT6_B7"] = 8.0
        rig.sim.run(1)
        rig.sim.ctl.commands.reset(OPERATOR)
        rig.sim.run(1)
        port = rig.gui.port
        first = request(port, "/api/events?limit=10")[1]
        ids = [r["id"] for r in first["rows"]]
        assert ids == sorted(ids, reverse=True) and len(ids) == 10
        older = request(port, f"/api/events?limit=10&before_id={first['next_before_id']}")[1]
        assert older["rows"] and max(r["id"] for r in older["rows"]) < min(ids)
        newest = first["newest_id"]
        rig.sim.ctl.commands.stop(OPERATOR)
        rig.sim.run(2)
        follow = request(port, f"/api/events?after_id={newest}&limit=100")[1]
        assert follow["rows"] and all(r["id"] > newest for r in follow["rows"])
        # Two log windows with different filters, served at the same time.
        results = {}
        def window(name, query):
            results[name] = [request(port, f"/api/events?{query}")[1] for _ in range(20)]
        threads = [threading.Thread(target=window, args=("critical", "severity=CRITICAL")),
                   threading.Thread(target=window, args=("states", "kinds=STATE_TRANSITION"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert all(r["rows"] and r["rows"][0]["severity"] == "CRITICAL"
                   for r in results["critical"])
        assert all({x["kind"] for x in r["rows"]} == {"STATE_TRANSITION"}
                   for r in results["states"])
        # CSV export of the current filter.
        code, body, headers = request(port, "/api/export.csv?severity=CRITICAL", raw=True)
        assert code == 200 and "attachment" in headers["Content-Disposition"]
        rows = list(csv.DictReader(io.StringIO(body)))
        critical = request(port, "/api/events?severity=CRITICAL&limit=500")[1]["rows"]
        assert len(rows) == len(critical) > 0
        assert {r["severity"] for r in rows} == {"CRITICAL"}
        assert {"source", "clock_source", "utc", "clock_sync", "boot_id", "procedure"} <= set(rows[0])
        assert rows[0]["source"] == "SIMULATED"
        assert request(port, "/api/events?limit=abc")[0] == 400
    finally:
        rig.close()


@scenario(79, "Trends through the GUI: bounded points, units, run breaks")
def s79():
    database = temp_db()
    rig = Rig(database=database, control={"logging.periodic_interval_s": 1.0})
    try:
        rig.sim.run(60)
        rig.poll()
        live = request(rig.gui.port, "/api/live")[1]["status"]
        query = (f"/api/trend?series=PT3,FLOW3,Y4_FEEDBACK&since=2000-01-01T00:00:00Z"
                 f"&until={live['utc'].replace('+', '%2B')}&max_points=20")
        code, trend = request(rig.gui.port, query)
        assert code == 200
        for name in ("PT3", "FLOW3", "Y4_FEEDBACK"):
            assert 0 < len(trend["series"][name]["points"]) <= 21
        assert trend["series"]["PT3"]["units"] == "degC"
        assert trend["stride"] > 1 and trend["samples_in_range"] >= 50
        assert request(rig.gui.port, "/api/trend?series=PT3")[0] == 400   # unbounded refused
    finally:
        rig.close()


@scenario(80, "Simulation runner: publisher and history up, fault injected, safe stop")
def s80():
    folder = temp_db().parent
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    command = [sys.executable, str(ROOT / "tools" / "run_simulation.py"), "--speed", "20",
               "--minutes", "0.1", "--port", str(port), "--bath", "84",
               "--database", str(folder / "sim.sqlite"), "--state", str(folder / "state.json"),
               "--fault", "pt3_invalid@5+10"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, cwd=ROOT)
    seen = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and seen is None:
        time.sleep(0.3)
        try:
            code, status = request(port, "/status")
            if code == 200:
                seen = status
        except OSError:
            pass
    output = process.communicate(timeout=30)[0]
    assert process.returncode == 0, output
    assert seen is not None and seen["data_source"] == "SIMULATED", output
    assert "fault pt3_invalid ON" in output and "safe frame written True" in output, output
    assert (folder / "sim.sqlite").exists()


@scenario(81, "One listener per address and port on every platform; bind errors by code")
def s81():
    # The servers never ask the OS to share a listening port. On Windows,
    # SO_REUSEADDR would let a second listener bind silently, so it is off there.
    assert QuietServer.allow_reuse_address is (os.name != "nt")
    # Error codes, not message text: POSIX errno and Winsock codes as Windows reports them.
    windows_in_use = OSError(10048, "Only one usage of each socket address is normally permitted")
    windows_in_use.winerror = 10048
    windows_denied = OSError(10013, "An attempt was made to access a socket in a way forbidden")
    windows_denied.winerror = 10013
    assert bind_error_category(OSError(errno.EADDRINUSE, "anything")) == ADDRESS_IN_USE
    assert bind_error_category(windows_in_use) == ADDRESS_IN_USE
    assert bind_error_category(windows_denied) == ACCESS_DENIED
    assert bind_error_category(OSError(errno.EACCES, "anything")) == ACCESS_DENIED
    assert bind_error_category(OSError(errno.EADDRNOTAVAIL, "anything")) == BIND_FAILED
    # Publisher, then GUI server, on the same port: the second is refused, the first serves on.
    publisher = StatusPublisher(port=0)
    second = gui = None
    assert publisher.start(), publisher.error
    try:
        second = StatusPublisher(port=publisher.port)
        _assert_refused_second_bind(second, publisher)
        gui = GuiServer(ControllerLink("http://127.0.0.1:9"), temp_db(), port=publisher.port)
        try:
            gui.start()
            raise AssertionError(f"GUI server bound port {publisher.port} while the publisher listens")
        except OSError as error:
            assert bind_error_category(error) == ADDRESS_IN_USE, (error.errno, error)
        assert request(publisher.port, "/health") == (200, {"ok": True})
        # After a stop with served connections, the same port can be used again at once.
        port = publisher.port
        publisher.stop()
        publisher = StatusPublisher(port=port)
        assert publisher.start(), (publisher.error_category, publisher.error_code, publisher.error)
        assert request(port, "/health") == (200, {"ok": True})
    finally:
        if gui is not None:
            gui.stop()
        if second is not None:
            second.stop()
        publisher.stop()
