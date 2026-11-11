"""Production controller with outputs enabled: writer, AUTO and MANUAL,
commands, logins, request lifecycle, shutdown paths and watchdogs.

The RevPi library is the recording fake from scenarios.shadow: a writable
instance records every output it sets, a monitoring instance refuses writes.
Runner tests start tools/run_controller.py exactly as the systemd unit does
(no test options in the product); an unhandled error is injected by a wrapper
script that patches PlantController.cycle.
"""

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict

from gui.auth import UserStore
from gui.link import ControllerLink
from gui.server import GuiServer
from pasteurizer.command_server import CommandServer, read_token_file
from pasteurizer.config import load_config
from pasteurizer.controller import PlantController
from pasteurizer.hardware_outputs import ALL_OFF, output_channels, y4_raw
from pasteurizer.modes import Mode
from pasteurizer.operator_interface import Role, User
from pasteurizer.output_commands import OutputCommands
from pasteurizer.run_lock import RunLock
from scenarios.harness import FAST, OPERATOR, Sim, scenario
from scenarios.historian import temp_db
from scenarios.shadow import fake_revpi_dir, free_port, load_fake_revpi

ROOT = Path(__file__).resolve().parents[2]
COMMISSIONER = User("comm", Role.COMMISSIONING)
PASSWORD = "correct horse battery"


def manual_sim(bath=25.0, mode=Mode.MANUAL, simulated_io=True) -> Sim:
    """A Sim with the selector in MANUAL (requests need it)."""
    sim = Sim(params=FAST, bath=bath, simulated_io=simulated_io)
    sim.plant.mode = mode
    return sim


def post(port, path, body=None, *, headers=None, cookie=None):
    """(status, json body, response headers)."""
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    sent = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        sent["Cookie"] = cookie
    try:
        connection.request("POST", path, json.dumps(body or {}), sent)
        response = connection.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None), dict(response.getheaders())
    finally:
        connection.close()


GUI = {"X-Requested-With": "mwt-gui"}


def get(port, path, cookie=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def login(port, name, password=PASSWORD):
    code, body, headers = post(port, "/api/login", {"name": name, "password": password}, headers=GUI)
    cookie = headers.get("Set-Cookie", "")
    return code, body, cookie.split(";")[0] if cookie else None, cookie


class ScanLoop:
    """Runs a Sim's controller in a thread with a command server, like the runner."""

    def __init__(self, sim, token_path):
        self.sim, self.commands = sim, CommandServer(token_path=token_path, port=0)
        assert self.commands.start()
        self.scan_thread = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.scan_thread = threading.get_ident()
        while not self._stop.is_set():
            self.commands.execute_pending(self.sim.ctl)
            self.sim.run(0.2)
            time.sleep(0.01)

    def stop(self):
        self._stop.set()
        self._thread.join(5)
        self.commands.stop()


@scenario(88, "Hardware output writer: whole frame to the mapped channels, Y4 scaled, all off at open and close")
def s88():
    config = load_config()
    channels = output_channels(config)
    assert set(channels) == {f for f in asdict(OutputCommands()) if f != "control_valve_percent"}
    assert sorted(channels.values()) == sorted(config.io_mapping["digital_outputs"].values())
    assert channels["heater_1kw"] == "O_1" and channels["transfer_pump_1"] == "O_9"
    assert (y4_raw(config, 0.0), y4_raw(config, 100.0), y4_raw(config, 50.0)) == (4000, 20000, 12000)
    fake, restore = load_fake_revpi()
    try:
        from pasteurizer.hardware_outputs import HardwareOutputWriter
        writer = HardwareOutputWriter(config)
        assert fake.RevPiModIO.instances[0].kwargs == {"autorefresh": False, "monitoring": False}
        assert all(fake.OUTPUTS[c] is False for c in channels.values())
        assert fake.OUTPUTS["OutputValue_1"] == 4000, "all off written at open"
        assert writer.write(OutputCommands(heater_1kw=True, pasteurizer_isolation_open=True,
                                           control_valve_percent=50.0, light_green=True))
        assert fake.OUTPUTS["O_1"] is True and fake.OUTPUTS["O_11"] is True
        assert fake.OUTPUTS["O_5"] is True and fake.OUTPUTS["O_2"] is False
        assert fake.OUTPUTS["OutputValue_1"] == 12000
        assert writer.last_frame.heater_1kw is True
        try:
            writer.write({"heater_1kw": True})
            raise AssertionError("writer accepted something other than a frame")
        except TypeError:
            pass
        writer.close()
        assert all(fake.OUTPUTS[c] is False for c in channels.values())
        assert fake.OUTPUTS["OutputValue_1"] == 4000, "all off written at close"
        assert not any(line.startswith("REFUSED") for line in fake.WRITES)
    finally:
        restore()


@scenario(89, "Production outputs: AUTO writes the agreed logic in every mode; no MANUAL-only gate")
def s89():
    sim = manual_sim(bath=40.0, mode=Mode.AUTO)          # cold: AUTO wants to preheat
    sim.run(15)                                           # after the start grace
    st = sim.status
    assert st.commanded["heater_3kw"] is True and sim.plant.outputs.heater_3kw is True, \
        "AUTO switches the heater it commands"
    assert st.written == st.commanded and st.output_write == "WRITTEN"
    assert not hasattr(st, "output_gate") and not hasattr(st, "output_policy")
    assert not sim.events("OUTPUT_GATE")
    for mode in (Mode.OFF, Mode.MANUAL):
        sim.plant.mode = mode
        sim.run(1)
        assert sim.plant.outputs.heater_3kw is False, f"{mode}: AUTO process stopped"
        assert sim.status.written == sim.status.commanded
    sim.plant.manhole_water = True                        # drain works in OFF too
    sim.plant.mode = Mode.OFF
    sim.run(11)
    assert sim.plant.outputs.manhole_drain is True and sim.status.written["manhole_drain"]
    try:
        PlantController(sim.config, reader=sim.plant, writer=sim.plant, store=sim.store,
                        event_log=sim.log, clock=sim.clock, output_policy="MANUAL_ONLY")
        raise AssertionError("the commissioning gate option still exists")
    except TypeError:
        pass


@scenario(90, "Manual requests expire unless renewed; release and clear switch off at once; roles enforced")
def s90():
    sim = manual_sim()
    sim.run(1)
    assert sim.ctl.commands.manual_request(COMMISSIONER, "heater_1kw", True).accepted
    sim.run(20)
    assert sim.plant.outputs.heater_1kw is True
    assert sim.ctl.commands.manual_request(COMMISSIONER, "heater_1kw", True).accepted  # renew
    sim.run(25)
    assert sim.plant.outputs.heater_1kw is True, "renewed request still on"
    sim.run(6)
    assert sim.plant.outputs.heater_1kw is False, "expired 30 s after the renewal"
    assert any(r["kind"] == "MANUAL_EXPIRED" for r in sim.history.events)
    refused = sim.ctl.commands.manual_request(OPERATOR, "heater_1kw", True)
    assert not refused.accepted and "not permitted" in refused.reason
    sim.ctl.commands.manual_request(COMMISSIONER, "heater_1kw", True)
    sim.ctl.commands.manual_request(COMMISSIONER, "pasteurizer_isolation_open", True)
    sim.run(1)
    assert sim.plant.outputs.heater_1kw and sim.plant.outputs.pasteurizer_isolation_open
    assert sim.ctl.commands.manual_release(OPERATOR, "heater_1kw").accepted   # anyone may switch off
    sim.run(0.4)
    assert not sim.plant.outputs.heater_1kw and sim.plant.outputs.pasteurizer_isolation_open
    assert sim.ctl.commands.manual_clear(OPERATOR).accepted
    sim.run(0.4)
    assert not sim.plant.outputs.pasteurizer_isolation_open
    actions = [r["data"]["action"] for r in sim.history.events if r["kind"] == "OPERATOR_ACTION"]
    assert {"MANUAL_REQUEST", "MANUAL_RELEASE", "MANUAL_CLEAR"} <= set(actions)


@scenario(97, "Renewals extend only active requests: never after expiry, clear, mode change or restart")
def s97():
    sim = manual_sim()
    sim.run(1)
    cmd = sim.ctl.commands
    other = User("comm2", Role.COMMISSIONING)
    assert cmd.manual_request(COMMISSIONER, "pasteurizer_isolation_open", True).accepted
    sim.run(20)
    renewed = cmd.manual_request(COMMISSIONER, "pasteurizer_isolation_open", True, renew=True)
    assert renewed.accepted and renewed.reason == "Request renewed"
    sim.run(25)
    assert sim.plant.outputs.pasteurizer_isolation_open, "renewed: still open 45 s after the press"
    for user, value in ((COMMISSIONER, False), (other, True)):    # other value or user
        refused = cmd.manual_request(user, "pasteurizer_isolation_open", value, renew=True)
        assert not refused.accepted and "press again" in refused.reason
    sim.run(6)
    assert not sim.plant.outputs.pasteurizer_isolation_open, "expired 30 s after the renewal"
    late = cmd.manual_request(COMMISSIONER, "pasteurizer_isolation_open", True, renew=True)
    assert not late.accepted and "press again" in late.reason, "an expired request stays gone"
    sim.run(1)
    assert not sim.plant.outputs.pasteurizer_isolation_open
    # Cleared by ALL OFF, or by a mode change: a renewal cannot bring it back.
    for clear in (lambda: cmd.manual_clear(OPERATOR), None):
        assert cmd.manual_request(COMMISSIONER, "pasteurizer_isolation_open", True).accepted
        sim.run(1)
        if clear:
            clear()
        else:
            sim.plant.mode = Mode.OFF
            sim.run(1)
            sim.plant.mode = Mode.MANUAL
        sim.run(1)
        assert not cmd.manual_request(COMMISSIONER, "pasteurizer_isolation_open", True,
                                      renew=True).accepted
        assert not sim.plant.outputs.pasteurizer_isolation_open
    # Overrides and the valve-test allowance follow the same rule.
    live = manual_sim(simulated_io=False)
    live.run(1)
    sw = live.config.level_roles["manhole_water_present"]
    assert not live.ctl.commands.level_override(COMMISSIONER, sw, True, renew=True).accepted
    assert live.ctl.commands.level_override(COMMISSIONER, sw, True).accepted
    assert live.ctl.commands.level_override(COMMISSIONER, sw, True, renew=True).accepted
    assert not live.ctl.commands.allow_valve_test(COMMISSIONER, renew=True).accepted
    assert live.ctl.commands.allow_valve_test(COMMISSIONER).accepted
    assert live.ctl.commands.allow_valve_test(COMMISSIONER, renew=True).accepted
    assert live.ctl.commands.manual_request(COMMISSIONER, "heater_1kw", True).accepted
    live.run(1)
    # Controller restart: requests, overrides and allowance live in memory only.
    after = live.restart(params=FAST, simulated_io=False)
    after.plant.mode = Mode.MANUAL
    after.run(1)
    m = after.ctl.manual
    assert m.active_requests() == {} and not m.overrides and m.valve_test is None
    assert not after.ctl.commands.manual_request(COMMISSIONER, "heater_1kw", True, renew=True).accepted
    assert not after.ctl.commands.level_override(COMMISSIONER, sw, True, renew=True).accepted
    assert not after.ctl.commands.allow_valve_test(COMMISSIONER, renew=True).accepted
    after.run(12)
    assert not after.plant.outputs.heater_1kw, "nothing switched on after the restart"
    renewals = [r["data"] for r in after.events("OPERATOR_ACTION") if r["data"]["action"] == "MANUAL_RENEW"]
    assert renewals and renewals[-1]["accepted"] is False


@scenario(98, "Manhole drain in MANUAL: STOP inhibits while active; ends on water event, release, expiry, mode change")
def s98():
    sim = manual_sim()
    cmd = sim.ctl.commands
    sim.plant.p.drain_time_s = 1e9                        # water stays until we change it
    sim.plant.manhole_water = True
    sim.run(11)
    assert sim.out("manhole_drain"), "MANUAL: automatic drainage after 10 s of water"
    assert cmd.manual_request(COMMISSIONER, "manhole_drain", False).accepted     # STOP
    sim.run(0.4)
    assert not sim.out("manhole_drain"), "STOP inhibits at once"
    sim.run(20)
    assert cmd.manual_request(COMMISSIONER, "manhole_drain", False, renew=True).accepted
    sim.run(5)
    assert not sim.out("manhole_drain"), "renewed STOP still inhibits"
    # A new water event ends the STOP and withdraws the request.
    sim.plant.manhole_water = False
    sim.run(1)
    sim.plant.manhole_water = True
    sim.run(0.4)
    assert "manhole_drain" not in sim.ctl.manual.active_requests()
    events = sim.events("MANUAL_RELEASE_AUTOMATIC")
    assert events and events[-1]["data"]["user"] == "comm", events
    assert not cmd.manual_request(COMMISSIONER, "manhole_drain", False, renew=True).accepted, \
        "the page's renewal cannot restore a used-up STOP"
    sim.run(11)
    assert sim.out("manhole_drain"), "automatic drainage resumed after the water event"
    # A new press inhibits again; release (Automatic) resumes at once.
    assert cmd.manual_request(COMMISSIONER, "manhole_drain", False).accepted
    sim.run(0.4)
    assert not sim.out("manhole_drain")
    assert cmd.manual_release(COMMISSIONER, "manhole_drain").accepted
    sim.run(11)
    assert sim.out("manhole_drain"), "released: automatic again"
    # Expiry ends it.
    assert cmd.manual_request(COMMISSIONER, "manhole_drain", False).accepted
    sim.run(29)
    assert not sim.out("manhole_drain")
    sim.run(13)
    assert sim.out("manhole_drain"), "STOP expired 30 s after the press"
    # Leaving MANUAL ends it: in OFF the drain works by its level logic.
    assert cmd.manual_request(COMMISSIONER, "manhole_drain", False).accepted
    sim.run(1)
    assert not sim.out("manhole_drain")
    sim.plant.mode = Mode.OFF
    sim.run(11)
    assert sim.out("manhole_drain"), "OFF: a STOP from MANUAL does not survive the mode change"
    # START (True) is automatic, never a run without water.
    sim.plant.mode = Mode.MANUAL
    sim.plant.manhole_water = False
    sim.run(25)
    assert cmd.manual_request(COMMISSIONER, "manhole_drain", True).accepted
    sim.run(15)
    assert not sim.out("manhole_drain"), "no manual run without water"
    # Commissioning test of the drain pump: LS1 override 'reached' (not a protective switch).
    live = manual_sim(simulated_io=False)
    live.run(1)
    sw = live.config.level_roles["manhole_water_present"]
    assert live.ctl.commands.level_override(COMMISSIONER, sw, True).accepted
    for _ in range(3):
        live.run(4)
        assert live.ctl.commands.level_override(COMMISSIONER, sw, True, renew=True).accepted
    assert live.out("manhole_drain"), "override renewed for 12 s: drain runs"
    live.run(31)                                          # override expired 10 s; run-on 20 s
    assert not live.out("manhole_drain")


@scenario(91, "Command server: localhost, token, commands executed in the scan thread, results returned")
def s91():
    try:
        CommandServer(token_path=temp_db().parent / "t", host="0.0.0.0", port=0)
        raise AssertionError("command server accepted a non-local address")
    except ValueError:
        pass
    sim = manual_sim()
    token_path = temp_db().parent / "command.token"
    loop = ScanLoop(sim, token_path)
    ran_in = []
    original = sim.ctl.commands.manual_request

    def recording(*args, **kwargs):
        ran_in.append(threading.get_ident())
        return original(*args, **kwargs)
    sim.ctl.commands.manual_request = recording
    try:
        port, token = read_token_file(token_path)
        if os.name != "nt":
            assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        body = {"user": "comm", "role": "COMMISSIONING", "action": "manual_request",
                "args": {"command": "heater_1kw", "value": True}}
        assert post(port, "/command", body)[0] == 403                          # no token
        assert post(port, "/command", body, headers={"X-Command-Token": "x"})[0] == 403
        code, result, _ = post(port, "/command", body, headers={"X-Command-Token": token})
        assert code == 200 and result["accepted"] is True, result
        assert ran_in == [loop.scan_thread], "executed in the scan thread"
        bad = dict(body, action="format_disk")
        assert post(port, "/command", bad, headers={"X-Command-Token": token})[0] == 400
        bad = dict(body, role="ADMIN")
        assert post(port, "/command", bad, headers={"X-Command-Token": token})[0] == 400
        time.sleep(0.6)
        assert sim.plant.outputs.heater_1kw is True
    finally:
        loop.stop()
    assert not token_path.exists(), "token removed at stop"


@scenario(92, "GUI logins: hashed passwords, lockout, strict cookie; commands need login and header")
def s92():
    folder = Path(tempfile.mkdtemp())
    users = UserStore(folder / "users.json")
    users.set_user("comm", "COMMISSIONING", PASSWORD)
    users.set_user("op", "OPERATOR", PASSWORD)
    stored = (folder / "users.json").read_text(encoding="utf-8")
    assert PASSWORD not in stored
    if os.name != "nt":
        assert stat.S_IMODE((folder / "users.json").stat().st_mode) == 0o600
    try:
        users.set_user("short", "OPERATOR", "abc")
        raise AssertionError("short password accepted")
    except ValueError:
        pass
    sim = manual_sim()
    token_path = folder / "command.token"
    loop = ScanLoop(sim, token_path)
    gui = GuiServer(ControllerLink("http://127.0.0.1:9"), temp_db(), port=0,
                    users=folder / "users.json", command_token=token_path)
    gui.start()
    command = {"action": "manual_request", "args": {"command": "heater_1kw", "value": True}}
    try:
        port = gui.port
        assert post(port, "/api/login", {"name": "comm", "password": PASSWORD})[0] == 403  # header
        started = time.monotonic()
        code, _, cookie, _ = login(port, "comm", "wrong password!")
        assert code == 401 and cookie is None and time.monotonic() - started >= 0.9
        code, body, cookie, raw = login(port, "comm")
        assert code == 200 and body == {"user": "comm", "role": "COMMISSIONING"}
        assert "HttpOnly" in raw and "SameSite=Strict" in raw and "Path=/" in raw
        assert get(port, "/api/session", cookie)[1]["user"] == "comm"
        assert post(port, "/api/command", command, headers=GUI)[0] == 401          # no session
        assert post(port, "/api/command", command, cookie=cookie)[0] == 403        # no header
        code, result, _ = post(port, "/api/command", command, headers=GUI, cookie=cookie)
        assert code == 200 and result["accepted"] is True, result
        time.sleep(0.6)
        assert sim.plant.outputs.heater_1kw is True
        logged = [r["data"] for r in sim.history.events if r["kind"] == "OPERATOR_ACTION"]
        assert logged[-1]["user"] == "comm" and logged[-1]["role"] == "COMMISSIONING"
        # An OPERATOR may log in but not request equipment.
        _, _, op_cookie, _ = login(port, "op")
        code, result, _ = post(port, "/api/command", command, headers=GUI, cookie=op_cookie)
        assert code == 200 and result["accepted"] is False and "not permitted" in result["reason"]
        # Logout ends the session.
        post(port, "/api/logout", headers=GUI, cookie=cookie)
        assert post(port, "/api/command", command, headers=GUI, cookie=cookie)[0] == 401
        # Lockout after five failures, for that name only.
        for _ in range(5):
            login(port, "op", "wrong password!")
        assert login(port, "op")[0] == 429
        assert login(port, "comm")[0] == 200
    finally:
        gui.stop()
        loop.stop()
    # Controller gone: commands answered 503, not lost silently.
    gui = GuiServer(ControllerLink("http://127.0.0.1:9"), temp_db(), port=0,
                    users=folder / "users.json", command_token=token_path)
    gui.start()
    try:
        _, _, cookie, _ = login(gui.port, "comm")
        assert post(gui.port, "/api/command", command, headers=GUI, cookie=cookie)[0] == 503
    finally:
        gui.stop()


@scenario(99, "Session end withdraws that user's requests: logout, idle and max-age expiry, GUI stop")
def s99():
    from gui import auth
    folder = Path(tempfile.mkdtemp())
    users = UserStore(folder / "users.json")
    for name, role in (("comm", "COMMISSIONING"), ("comm2", "COMMISSIONING"), ("op", "OPERATOR")):
        users.set_user(name, role, PASSWORD)
    sim = manual_sim(simulated_io=False)
    token_path = folder / "command.token"
    loop = ScanLoop(sim, token_path)
    gui = GuiServer(ControllerLink("http://127.0.0.1:9"), temp_db(), port=0,
                    users=folder / "users.json", command_token=token_path)
    t = [1000.0]
    gui.sessions.clock = lambda: t[0]
    gui.start()
    sw = sim.config.level_roles["manhole_water_present"]

    def press(cookie, action, args):
        code, result, _ = post(gui.port, "/api/command", {"action": action, "args": args},
                               headers=GUI, cookie=cookie)
        assert code == 200 and result["accepted"], (action, result)

    def holders():
        m = sim.ctl.manual
        return ({k: v.user for k, v in m.requests.items()},
                {k: v.user for k, v in m.overrides.items()},
                m.valve_test.user if m.valve_test else None)
    try:
        time.sleep(0.5)
        cookies = {name: login(gui.port, name)[2] for name in ("comm", "comm2", "op")}
        press(cookies["comm"], "manual_request", {"command": "pasteurizer_isolation_open", "value": True})
        press(cookies["comm"], "level_override", {"switch": sw, "value": True})
        press(cookies["comm"], "allow_valve_test", {})
        press(cookies["comm2"], "manual_request", {"command": "heater_1kw", "value": True})
        assert holders() == ({"pasteurizer_isolation_open": "comm", "heater_1kw": "comm2"},
                             {sw: "comm"}, "comm")
        # Logout of comm withdraws only comm's requests, override and allowance.
        post(gui.port, "/api/logout", headers=GUI, cookie=cookies["comm"])
        assert holders() == ({"heater_1kw": "comm2"}, {}, None), holders()
        assert gui.session_clears[-1][:3] == ("comm", "LOGOUT", 200)
        time.sleep(0.6)
        assert not sim.plant.outputs.pasteurizer_isolation_open
        # An operator's logout sends nothing (operators hold no requests).
        post(gui.port, "/api/logout", headers=GUI, cookie=cookies["op"])
        assert [c[0] for c in gui.session_clears] == ["comm"]
        # Idle expiry (30 min without a request) ends comm2's session on the sweep.
        t[0] += auth.IDLE_S + 1
        assert gui.sessions.sweep() == 1
        assert holders() == ({}, {}, None)
        assert gui.session_clears[-1][:3] == ("comm2", "IDLE_EXPIRED", 200)
        # Maximum age: a session renewed all the time still ends after 8 h.
        cookie = login(gui.port, "comm")[2]
        press(cookie, "manual_request", {"command": "pasteurizer_isolation_open", "value": True})
        for _ in range(int(auth.MAX_AGE_S / auth.IDLE_S) + 1):
            t[0] += auth.IDLE_S - 60
            assert get(gui.port, "/api/session", cookie)[1].get("user") in ("comm", None)
        assert get(gui.port, "/api/session", cookie)[1].get("user") is None
        assert gui.session_clears[-1][:2] == ("comm", "MAX_AGE_EXPIRED")
        assert "pasteurizer_isolation_open" not in sim.ctl.manual.requests
        # GUI stop ends every session.
        cookie = login(gui.port, "comm2")[2]
        press(cookie, "manual_request", {"command": "heater_1kw", "value": True})
        gui.stop()
        gui = None
        assert holders() == ({}, {}, None)
        clears = [r["data"] for r in sim.events("OPERATOR_ACTION")
                  if r["data"]["action"] == "MANUAL_CLEAR"]
        assert [c["cause"] for c in clears] == ["LOGOUT", "IDLE_EXPIRED", "MAX_AGE_EXPIRED",
                                                "GUI_STOPPED"], clears
        assert all(c["own"] is True and c["accepted"] for c in clears)
    finally:
        if gui:
            gui.stop()
        loop.stop()


def run_controller(folder, values, minutes=None, extra=(), env_extra=None, script=None):
    """Start tools/run_controller.py on the fake RevPi as the systemd unit does
    (outputs enabled); script: a wrapper to run instead (fault injection)."""
    env = dict(os.environ, PYTHONPATH=str(fake_revpi_dir()),
               FAKE_REVPI_WRITE_LOG=str(folder / "writes.txt"),
               FAKE_REVPI_OUTPUTS=str(folder / "outputs.json"),
               FAKE_REVPI_VALUES=json.dumps(values), **(env_extra or {}))
    port, command_port = free_port(), free_port()
    command = [sys.executable, str(script or ROOT / "tools" / "run_controller.py"),
               "--port", str(port), "--command-port", str(command_port),
               "--database", str(folder / "controller.sqlite"),
               "--state", str(folder / "controller_state.json"),
               "--lock", str(folder / "controller.lock"), "--token", str(folder / "command.token"),
               *(("--minutes", str(minutes)) if minutes is not None else ()), *extra]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, cwd=ROOT, env=env)
    return process, port


def safe_outputs(folder, service_result=None):
    env = dict(os.environ, PYTHONPATH=str(fake_revpi_dir()),
               FAKE_REVPI_OUTPUTS=str(folder / "outputs.json"),
               FAKE_REVPI_WRITE_LOG=str(folder / "writes.txt"))
    env.pop("SERVICE_RESULT", None)
    if service_result is not None:
        env.update(SERVICE_RESULT=service_result, EXIT_CODE="killed", EXIT_STATUS="KILL")
    return subprocess.run([sys.executable, str(ROOT / "tools" / "safe_outputs.py"),
                           "--lock", str(folder / "controller.lock"),
                           "--state", str(folder / "controller_state.json")],
                          capture_output=True, text=True, cwd=ROOT, timeout=30, env=env)


def command(folder, action, args=None, user="comm", role="COMMISSIONING"):
    port, token = read_token_file(folder / "command.token")
    body = {"user": user, "role": role, "action": action, "args": args or {}}
    return post(port, "/command", body, headers={"X-Command-Token": token})[1]


def open_y3(folder):
    """Selector in MANUAL: open Y3 (O_11) through the command server."""
    assert wait_for(lambda: read_token_file(folder / "command.token")), "controller did not start"
    result = command(folder, "manual_request",
                     {"command": "pasteurizer_isolation_open", "value": True})
    assert result["accepted"], result
    assert wait_for(lambda: outputs(folder)["O_11"]), "Y3 (O_11) open"


def all_off(folder):
    current = outputs(folder)
    return (not any(current.get(c) for c in output_channels(load_config()).values())
            and current.get("OutputValue_1") == 4000)


def state_file(folder):
    path = folder / "controller_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def events(folder, kind):
    from scenarios.harness import stored_rows
    return [r for r in stored_rows(folder / "controller.sqlite", "events") if r["kind"] == kind]


class NotifySocket:
    """Stands in for systemd's NOTIFY_SOCKET (Linux): collects the datagrams."""

    def __init__(self, folder):
        self.path = str(folder / "notify.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(0.2)
        self.messages = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.messages.append((time.monotonic(), self.sock.recv(4096).decode()))
            except socket.timeout:
                pass

    def env(self):
        return {"NOTIFY_SOCKET": self.path, "WATCHDOG_USEC": "3000000"}

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self.sock.close()

    def kinds(self):
        return [m.split("\n")[0].split("=")[0] for _, m in self.messages]


def wait_for(predicate, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.2)
    return None


def outputs(folder):
    """The fake's output file (rewritten on every write: retry a half-written read)."""
    for _ in range(20):
        try:
            return json.loads((folder / "outputs.json").read_text(encoding="utf-8"))
        except ValueError:
            time.sleep(0.02)
    raise ValueError("outputs.json unreadable")


@scenario(93, "End to end on fake RevPi: log in, a manual command switches its output, stop leaves all off")
def s93():
    folder = Path(tempfile.mkdtemp())
    UserStore(folder / "users.json").set_user("comm", "COMMISSIONING", PASSWORD)
    process, port = run_controller(folder, {"I_3": True, "I_4": False}, 0.45)   # S1 in MANUAL
    gui = None
    try:
        status = wait_for(lambda: get(port, "/status")[1])
        assert status, "controller did not start"
        assert status["outputs_enabled"] is True and status["mode"] == "MANUAL"
        assert "output_gate" not in status and status["output_write"] == "WRITTEN"
        assert not any(outputs(folder)[c] for c in output_channels(load_config()).values()), \
            "all off right after start"
        gui = GuiServer(ControllerLink(f"http://127.0.0.1:{port}"), folder / "controller.sqlite",
                        port=0, users=folder / "users.json", command_token=folder / "command.token")
        gui.start()
        _, _, cookie, _ = login(gui.port, "comm")
        for name, value in (("heater_1kw", True), ("pasteurizer_isolation_open", True)):
            code, result, _ = post(gui.port, "/api/command", {"action": "manual_request",
                                   "args": {"command": name, "value": value}},
                                   headers=GUI, cookie=cookie)
            assert code == 200 and result["accepted"], result
        # Heaters wait 10 s after start (switching pause); the page renews every 10 s.
        assert wait_for(lambda: outputs(folder)["O_11"]), "Y3 (O_11) switched"
        code, result, _ = post(gui.port, "/api/command", {"action": "manual_request",
                               "args": {"command": "heater_1kw", "value": True}},
                               headers=GUI, cookie=cookie)
        assert wait_for(lambda: outputs(folder)["O_1"], 15), "heater 1 kW (O_1) switched after the pause"
        status = get(port, "/status")[1]
        assert status["written"]["heater_1kw"] is True and status["commanded"]["heater_1kw"] is True
        code, result, _ = post(gui.port, "/api/command", {"action": "manual_release",
                               "args": {"command": "heater_1kw"}}, headers=GUI, cookie=cookie)
        assert result["accepted"] and wait_for(lambda: outputs(folder)["O_1"] is False)
    finally:
        if gui:
            gui.stop()
        output = process.communicate(timeout=60)[0]
    assert process.returncode == 0, output
    assert "OUTPUTS ENABLED: the controller writes the RevPi outputs in every mode" in output, output
    assert "then all outputs off" in output, output
    final = outputs(folder)
    assert not any(final[c] for c in output_channels(load_config()).values()), final
    assert final["OutputValue_1"] == 4000
    writes = (folder / "writes.txt").read_text(encoding="utf-8")
    assert "REFUSED" not in writes and writes.count("OPEN writable") == 1
    assert not (folder / "command.token").exists()


@scenario(94, "End to end: AUTO writes the agreed outputs (3 kW preheat); safe_outputs refuses while running")
def s94():
    folder = Path(tempfile.mkdtemp())
    process, port = run_controller(folder, {"I_3": False, "I_4": True, "I_8_i06": True}, 0.4)  # AUTO, supply
    channels = output_channels(load_config())
    try:
        status = wait_for(lambda: (lambda s: s if s["written"] and s["written"]["heater_3kw"] else None)(
            get(port, "/status")[1]), 20)
        assert status, "AUTO preheats after the start grace"
        assert status["commanded"]["heater_3kw"] and status["output_write"] == "WRITTEN"
        assert wait_for(lambda: outputs(folder)[channels["heater_3kw"]]), "3 kW heater (O_2) switched"
        current = outputs(folder)
        on = {name for name, channel in channels.items() if current[channel]}
        assert on == {name for name, value in status["written"].items()
                      if value is True}, (on, status["written"])
        # A safe_outputs run while the controller holds the lock writes nothing.
        result = safe_outputs(folder)
        assert result.returncode != 0 and "controller is running" in result.stdout + result.stderr
        assert outputs(folder)[channels["heater_3kw"]], "the running controller was not disturbed"
    finally:
        output = process.communicate(timeout=60)[0]
    assert process.returncode == 0, output
    assert all_off(folder), outputs(folder)


@scenario(95, "safe_outputs: switches every output off when no controller runs")
def s95():
    folder = Path(tempfile.mkdtemp())
    (folder / "outputs.json").write_text(json.dumps({"O_1": True}), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(fake_revpi_dir()),
               FAKE_REVPI_OUTPUTS=str(folder / "outputs.json"),
               FAKE_REVPI_WRITE_LOG=str(folder / "writes.txt"))
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "safe_outputs.py"),
                             "--lock", str(folder / "controller.lock")],
                            capture_output=True, text=True, cwd=ROOT, timeout=30, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    final = outputs(folder)
    assert not any(final[c] for c in output_channels(load_config()).values())
    assert final["OutputValue_1"] == 4000 and "All outputs off" in result.stdout
    lock = RunLock(folder / "controller.lock")
    assert lock.acquire(), "lock released afterwards"
    lock.release()
    assert ALL_OFF == OutputCommands()


@scenario(96, "Scan hangs with an output on: watchdog switches all off, latches off, runner stops (exit 3)")
def s96():
    folder = Path(tempfile.mkdtemp())
    env_values = {"I_3": True, "I_4": False}
    os.environ["FAKE_REVPI_STALL"] = json.dumps({"after": 40, "seconds": 4})
    try:
        process, port = run_controller(folder, env_values, 1.0)
    finally:
        del os.environ["FAKE_REVPI_STALL"]
    try:
        found = wait_for(lambda: read_token_file(folder / "command.token"))
        assert found, "controller did not start"
        command_port, token = found
        body = {"user": "comm", "role": "COMMISSIONING", "action": "manual_request",
                "args": {"command": "pasteurizer_isolation_open", "value": True}}
        assert post(command_port, "/command", body, headers={"X-Command-Token": token})[1]["accepted"]
        assert wait_for(lambda: outputs(folder)["O_11"]), "Y3 open before the hang"
    finally:
        output = process.communicate(timeout=60)[0]
    assert "WATCHDOG" in output, output
    assert process.returncode == 3, output              # systemd: failure -> restart
    final = outputs(folder)
    assert not any(final[c] for c in output_channels(load_config()).values()), final
    y3 = [json.loads(line[6:])["O_11"] for line in
          (folder / "writes.txt").read_text(encoding="utf-8").splitlines()
          if line.startswith("WRITE ") and "O_11" in line]
    first_on = y3.index(True)
    off_again = y3.index(False, first_on)
    assert not any(y3[off_again:]), "Y3 switched on again after the watchdog trip"
    state = json.loads((folder / "controller_state.json").read_text(encoding="utf-8"))
    assert state["watchdog_tripped"] is True, state


POSIX = os.name != "nt"
MANUAL_SELECTOR = {"I_3": True, "I_4": False}


def windows_note(what):
    print(f"      Windows: {what} is Linux/systemd behaviour; checked on Linux runs only")


@scenario(100, "Runner start and orderly stop (SIGINT = systemctl stop, SIGTERM): all off, state, systemd notify")
def s100():
    if not POSIX:
        return windows_note("stopping by SIGINT/SIGTERM and sd_notify")
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        folder = Path(tempfile.mkdtemp())
        notify = NotifySocket(folder)
        process, port = run_controller(folder, MANUAL_SELECTOR, env_extra=notify.env())
        try:
            open_y3(folder)
            writes = (folder / "writes.txt").read_text(encoding="utf-8").splitlines()
            assert writes[0] == "OPEN writable"
            first = json.loads(writes[1][6:])
            assert not any(first[c] for c in output_channels(load_config()).values()), \
                "all off written before the first scan"
            time.sleep(3.0)
            process.send_signal(stop_signal)
            output = process.communicate(timeout=30)[0]
        finally:
            if process.poll() is None:
                process.kill()
            notify.close()
        assert process.returncode == 0, output
        assert f"Stop requested ({stop_signal.name})" in output, output
        assert "safe frame written True, then all outputs off" in output, output
        assert "SHUTDOWN STEP FAILED" not in output, output
        assert all_off(folder), outputs(folder)
        assert not (state_file(folder) or {}).get("watchdog_tripped"), "orderly stop: not marked"
        stop = events(folder, "CONTROLLER_STOP")
        assert stop and stop[-1]["data"]["reason"] == "controller stopped", stop
        assert any(r["data"].get("cause") == "SHUTDOWN" for r in events(folder, "OUTPUT_WRITTEN"))
        kinds = notify.kinds()
        assert kinds[0] == "READY" and kinds[-1] == "STOPPING", kinds
        beats = [t for (t, m) in notify.messages if m == "WATCHDOG=1"]
        assert len(beats) >= 4 and max(b - a for a, b in zip(beats, beats[1:])) < 1.5, beats
        assert not (folder / "command.token").exists()
        lock = RunLock(folder / "controller.lock")
        assert lock.acquire(), "lock released"
        lock.release()


CRASH_WRAPPER = """
import os, runpy, sys
from pathlib import Path
ROOT = Path(os.environ["CRASH_ROOT"])
sys.path.insert(0, str(ROOT))
from pasteurizer.controller import plant_controller
original = plant_controller.PlantController.cycle
def cycle(self):
    if Path(os.environ["CRASH_TRIGGER"]).exists():
        raise RuntimeError("injected test failure in the scan")
    return original(self)
plant_controller.PlantController.cycle = cycle
sys.argv[0] = str(ROOT / "tools" / "run_controller.py")
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@scenario(101, "Unhandled error in the scan: orderly shutdown, all off, abnormal stop recorded, exit 1")
def s101():
    folder = Path(tempfile.mkdtemp())
    wrapper = folder / "crash_wrapper.py"
    wrapper.write_text(CRASH_WRAPPER, encoding="utf-8")
    trigger = folder / "crash.now"
    process, port = run_controller(folder, MANUAL_SELECTOR, script=wrapper,
                                   env_extra={"CRASH_TRIGGER": str(trigger), "CRASH_ROOT": str(ROOT)})
    try:
        open_y3(folder)
        trigger.write_text("x", encoding="utf-8")
        output = process.communicate(timeout=30)[0]
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 1, output
    assert "CONTROLLER ERROR: RuntimeError: injected test failure" in output, output
    assert "Traceback" in output and "safe frame written True, then all outputs off" in output, output
    assert all_off(folder), outputs(folder)
    assert state_file(folder)["watchdog_tripped"] is True, "next start latches WATCHDOG_RESTART"
    stop = events(folder, "CONTROLLER_STOP")
    assert stop[-1]["data"]["reason"] == "error: RuntimeError", stop


@scenario(102, "Killed (SIGKILL): outputs keep their value until ExecStopPost; restart latches WATCHDOG_RESTART")
def s102():
    folder = Path(tempfile.mkdtemp())
    process, port = run_controller(folder, MANUAL_SELECTOR)
    try:
        open_y3(folder)
        time.sleep(2.5)                         # the historian writer stores about every second
        process.kill()                          # SIGKILL / TerminateProcess: no shutdown runs
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert outputs(folder)["O_11"] is True, "a killed process leaves Y3 as last written"
    by_hand = safe_outputs(folder)              # run by hand: recovery only, nothing marked
    assert by_hand.returncode == 0 and all_off(folder), by_hand.stdout + by_hand.stderr
    assert not (state_file(folder) or {}).get("watchdog_tripped"), "run by hand: not marked"
    (folder / "outputs.json").write_text(json.dumps({"O_11": True}), encoding="utf-8")
    post_stop = safe_outputs(folder, service_result="signal")    # as systemd ExecStopPost
    assert post_stop.returncode == 0, post_stop.stdout + post_stop.stderr
    assert "abnormal stop marked" in post_stop.stdout and all_off(folder), post_stop.stdout
    assert safe_outputs(folder, service_result="success").returncode == 0
    assert state_file(folder)["watchdog_tripped"] is True, "a later clean ExecStopPost keeps the mark"
    # The restarted controller: plant fault latched, everything off, no manual requests.
    process, port = run_controller(folder, MANUAL_SELECTOR, 0.35)
    try:
        status = wait_for(lambda: get(port, "/status")[1])
        codes = {a["code"] for a in status["alarms"]}
        assert "WATCHDOG_RESTART" in codes, codes
        assert status["manual_requests"] == {}
        result = command(folder, "manual_request",
                         {"command": "pasteurizer_isolation_open", "value": True}, )
        time.sleep(1.0)
        assert not outputs(folder)["O_11"], "plant Level 3: nothing opens until a GUI reset"
        assert result["accepted"]                # accepted as a request, blocked by the trip
    finally:
        output = process.communicate(timeout=60)[0]
    assert process.returncode == 0, output
    assert all_off(folder)
    assert events(folder, "PREVIOUS_RUN_UNCLEAN"), "the unclean end is in the log"
    start = events(folder, "CONTROLLER_START")[-1]["data"]
    assert start["watchdog_restart"] is True


@scenario(103, "Scan stuck for good: watchdog latches all off, process ends itself; systemd heartbeats stop")
def s103():
    folder = Path(tempfile.mkdtemp())
    notify = NotifySocket(folder) if POSIX else None
    os.environ["FAKE_REVPI_STALL"] = json.dumps({"after": 25, "seconds": 120})
    try:
        process, port = run_controller(folder, MANUAL_SELECTOR,
                                       env_extra=notify.env() if notify else None)
    finally:
        del os.environ["FAKE_REVPI_STALL"]
    try:
        found = wait_for(lambda: read_token_file(folder / "command.token"))
        assert found, "controller did not start"
        result = command(folder, "manual_request",
                         {"command": "pasteurizer_isolation_open", "value": True})
        assert result["accepted"], result
        started = time.monotonic()
        output = process.communicate(timeout=60)[0]
        elapsed = time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()
        if notify:
            notify.close()
    assert process.returncode == 3, output
    assert "WATCHDOG: scan still stuck; ending the process" in output, output
    assert elapsed < 30, elapsed
    assert all_off(folder), outputs(folder)
    assert state_file(folder)["watchdog_tripped"] is True
    if notify:
        beats = [t for (t, m) in notify.messages if m == "WATCHDOG=1"]
        assert beats and "STOPPING" not in notify.kinds(), notify.kinds()
        assert notify.messages[-1][0] - beats[-1] < 1.0, "nothing after the last heartbeat"
        print(f"      heartbeats stopped {time.monotonic() - beats[-1]:.0f} s before the end of the "
              "test; systemd (WatchdogSec=3) would have killed the process after 3 s")


@scenario(104, "Watchdog pieces: inhibit latches even with the writer busy; abnormal-stop marker; notify no-op")
def s104():
    from pasteurizer.persistence import FileStore, PersistentState, mark_abnormal_stop
    from pasteurizer.systemd_notify import SystemdNotifier
    config = load_config()
    channels = output_channels(config)
    fake, restore = load_fake_revpi()
    try:
        from pasteurizer.hardware_outputs import HardwareOutputWriter
        writer = HardwareOutputWriter(config)
        assert writer.write(OutputCommands(pasteurizer_isolation_open=True, heater_1kw=True))
        writer._lock.acquire()                  # the scan thread is stuck inside a write
        started = time.monotonic()
        assert writer.inhibit(lock_timeout_s=0.2) is False, "busy: no all-off write now"
        assert time.monotonic() - started < 1.0, "the watchdog thread is not blocked"
        writer._lock.release()
        assert writer.inhibited
        assert writer.write(OutputCommands(heater_1kw=True)) is False, "later frames not written"
        assert not any(fake.OUTPUTS[c] for c in channels.values()), "but all off reaches the outputs"
        writer.close()
    finally:
        restore()
    folder = Path(tempfile.mkdtemp())
    path = folder / "state.json"
    assert mark_abnormal_stop(path) == "marked" and FileStore(path).load().watchdog_tripped
    assert mark_abnormal_stop(path) == "already marked"
    FileStore(path).save(PersistentState(latched_faults=["OVERTEMP"], stop_hold=True))
    assert mark_abnormal_stop(path) == "marked"
    kept = FileStore(path).load()
    assert kept.latched_faults == ["OVERTEMP"] and kept.stop_hold and kept.watchdog_tripped
    path.write_text("{broken", encoding="utf-8")
    assert mark_abnormal_stop(path).startswith("not marked"), "a corrupt file is left for FAULT_STORAGE"
    assert path.read_text(encoding="utf-8") == "{broken"
    quiet = SystemdNotifier(environ={})
    assert not quiet.active and quiet.ready() is False and quiet.watchdog() is False
    t = [0.0]
    abstract = SystemdNotifier(environ={"NOTIFY_SOCKET": "@mwt-test", "WATCHDOG_USEC": "3000000"},
                               clock=lambda: t[0])
    assert abstract.address == "\0mwt-test" and abstract.watchdog_s == 3.0
    abstract._send = lambda message: True
    assert abstract.watchdog() is True and abstract.watchdog() is False, "rate limited"
    t[0] = 0.6
    assert abstract.watchdog() is True


@scenario(105, "Flow used for control only with a valid, confirmed K-factor; unconfirmed flow shown, never used")
def s105():
    from pasteurizer.contracts import FlowSignal
    from pasteurizer.flows import FlowProcessor
    from pasteurizer.transfer_flow import measured_flow
    now = 100.0

    def signal(**changes):
        base = dict(raw_counter=10, pulse_rate_hz=20.0, flow_lpm=0.6, measured_volume_litres=1.0,
                    valid=True, calibrated=True, error=None, sampled_at=now)
        base.update(changes)
        return FlowSignal(**base)
    assert measured_flow(signal(), now) == 0.6
    assert measured_flow(signal(calibrated=False), now) is None, "unconfirmed factor"
    assert measured_flow(signal(flow_lpm=None, calibrated=False), now) is None, "no factor"
    assert measured_flow(signal(valid=False), now) is None
    assert measured_flow(signal(sampled_at=now - 3.0), now) is None, "stale"
    for bad in (dict(pulses_per_litre=0.0), dict(pulses_per_litre=float("inf")),
                dict(pulses_per_litre=None, calibration_confirmed=True)):
        try:
            FlowProcessor(**bad)
            raise AssertionError(f"invalid K-factor accepted: {bad}")
        except ValueError:
            pass
    assert all(f.pulses_per_litre is None and f.calibration_confirmed is False
               for f in load_config().flows.values()), "the repository's flow factors stay unassigned"
    # A provisional factor that is not confirmed: L/min is shown, the pump stays blocked.
    for confirmed in (True, False):
        sim = Sim(params=FAST, bath=25.0, flow_confirmed=confirmed)
        sim.plant.mode = Mode.MANUAL
        sim.run(1)
        assert sim.ctl.commands.manual_request(COMMISSIONER, "transfer_pump_1", True).accepted
        sim.run(12)                                       # 3 kW switching pause after start
        flow = sim.status.signals["flows"]["flow_sensor_1"]
        assert flow["calibrated"] is confirmed and flow["flow_lpm"] is not None, "L/min shown"
        if confirmed:
            assert sim.plant.outputs.transfer_pump_1 is True, "confirmed factor: pump runs"
            assert not sim.alarm("TRANSFER_1_FLOW_INVALID")
        else:
            assert sim.plant.outputs.transfer_pump_1 is False, "unconfirmed: pump stopped"
            assert sim.alarm("TRANSFER_1_FLOW_INVALID").active
            assert sim.ctl.commands.manual_request(COMMISSIONER, "transfer_pump_1", True,
                                                   renew=True).accepted
            sim.run(11)
            assert sim.alarm("TRANSFER_1_FLOW_INVALID_PERSISTENT"), "Level 2 after 10 s"
            assert sim.plant.outputs.transfer_pump_1 is False
