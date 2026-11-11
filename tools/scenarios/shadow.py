"""Shadow run: live inputs, outputs disabled (tools/run_controller.py --outputs disabled).

The RevPi library is replaced by a fake that records and refuses every write
attempt, so these tests prove that nothing in the shadow path writes an
output, and that the status, the log and the GUI never report a write.
"""

import ast
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict

from gui.history_api import describe
from gui.link import ControllerLink
from gui.server import GuiServer
from gui.tags import BY_ID
from pasteurizer.config import ConfigError, load_config
from pasteurizer.controller import PlantController
from pasteurizer.output_commands import OutputCommands
from pasteurizer.signals import SignalProcessor
from pasteurizer.shadow_outputs import ShadowOutputWriter
from scenarios.gui import request
from scenarios.harness import FAST, Sim, scenario

ROOT = Path(__file__).resolve().parents[2]
SHADOW_TEXT = "LIVE INPUTS — OUTPUTS DISABLED / SHADOW RUN"
NOT_WRITTEN = "not written — outputs disabled"

# A stand-in for revpimodio2: inputs readable, every write attempt recorded
# (to FAKE_REVPI_WRITE_LOG) and refused.
FAKE_REVPI = '''
import json
import os

VALUES = {"I_4": True, "InputValue_1": 4054, "OutputValue_1": 4000}
for _name in ("RTDValue_1", "RTDValue_2", "RTDValue_1_i04", "RTDValue_2_i04",
              "RTDValue_1_i05", "RTDValue_2_i05"):
    VALUES[_name] = 250                     # 25.0 C
for _name, _value in json.loads(os.environ.get("FAKE_REVPI_VALUES", "{}")).items():
    VALUES[_name] = _value
COUNTS = {}
READS = 0
RESOLVED = []
WRITES = []        # every write attempt (refused or done)
OUTPUTS = {}       # values set by a writable (monitoring=False) instance


def _log(line):
    WRITES.append(line)
    path = os.environ.get("FAKE_REVPI_WRITE_LOG")
    if path:
        with open(path, "a") as file:
            file.write(line + "\\n")


def _refuse(what):
    _log("REFUSED " + what)
    raise RuntimeError("fake RevPi: write attempted in monitoring mode: " + what)


class _Channel:
    def __init__(self, name, owner):
        self._name = name
        self._owner = owner

    @property
    def value(self):
        if self._name.startswith("Counter_"):
            COUNTS[self._name] = COUNTS.get(self._name, 1000) + 3
            return COUNTS[self._name]
        return VALUES.get(self._name, 0)

    @value.setter
    def value(self, new):
        if self._owner.kwargs.get("monitoring") is not False:
            _refuse(f"{self._name}={new}")
        self._owner.pending[self._name] = new


class _IO:
    def __init__(self, owner):
        self._owner = owner

    def __getitem__(self, name):
        RESOLVED.append(name)
        return _Channel(name, self._owner)


class RevPiModIO:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pending = {}
        self.io = _IO(self)
        RevPiModIO.instances.append(self)
        if kwargs.get("monitoring") is False:
            _log("OPEN writable")

    def readprocimg(self):
        global READS
        READS += 1
        stall = json.loads(os.environ.get("FAKE_REVPI_STALL", "null"))
        if stall and READS == stall["after"]:
            import time
            time.sleep(stall["seconds"])        # the scan hangs once
        return True

    def writeprocimg(self):
        if self.kwargs.get("monitoring") is not False:
            _refuse("writeprocimg")
        OUTPUTS.update(self.pending)
        _log("WRITE " + json.dumps(self.pending, sort_keys=True))
        path = os.environ.get("FAKE_REVPI_OUTPUTS")
        if path:
            with open(path, "w") as file:
                json.dump(OUTPUTS, file)
        return True

    def setdefaultvalues(self):
        _refuse("setdefaultvalues")

    def cleanup(self):
        pass
'''


def fake_revpi_dir() -> Path:
    folder = Path(tempfile.mkdtemp())
    (folder / "revpimodio2.py").write_text(FAKE_REVPI, encoding="utf-8")
    return folder


def load_fake_revpi():
    """Import the fake as revpimodio2 in this process; returns (module, restore)."""
    folder = fake_revpi_dir()
    saved = sys.modules.get("revpimodio2")
    spec = importlib.util.spec_from_file_location("revpimodio2", folder / "revpimodio2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["revpimodio2"] = module

    def restore():
        if saved is None:
            sys.modules.pop("revpimodio2", None)
        else:
            sys.modules["revpimodio2"] = saved
    return module, restore


def shadow_controller(sim) -> ShadowOutputWriter:
    """Replace the Sim's controller by one with the shadow writer (plant only read)."""
    writer = ShadowOutputWriter()
    sim.ctl = PlantController(sim.config, reader=sim.plant, writer=writer, store=sim.store,
                              event_log=sim.log, clock=sim.clock, simulated_io=True,
                              heartbeat=sim.watchdog.heartbeat)
    return writer


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@scenario(82, "Shadow writer cannot reach outputs: plant unchanged, nothing reported written")
def s82():
    # 1. The module itself: no hardware library, no process, no file or device access.
    source = (ROOT / "pasteurizer" / "shadow_outputs.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            raise AssertionError("shadow writer opens a file")
    assert imported == {"pasteurizer.output_commands"}, imported
    assert "piTest" not in source and "piControl" not in source
    # 2. Writing with a fake RevPi library present: the library is never touched.
    fake, restore = load_fake_revpi()
    try:
        writer = ShadowOutputWriter()
        assert writer.write(OutputCommands(heater_3kw=True, transfer_pump_1=True)) is True
        assert writer.outputs_enabled is False and writer.frames_received == 1
        assert fake.RevPiModIO.instances == [] and fake.WRITES == [] and fake.RESOLVED == []
    finally:
        restore()
    try:
        writer.write({"heater_3kw": True})
        raise AssertionError("writer accepted something other than a command frame")
    except TypeError:
        pass
    # 3. A controller with the shadow writer: commands computed, the plant
    #    (standing in for the hardware) never receives a single output.
    sim = Sim(params=FAST, bath=84.0)
    sim.plant.tank[1] = 900.0
    untouched = asdict(sim.plant.outputs)
    writer = shadow_controller(sim)
    assert sim.ctl.outputs_enabled is False
    sim.run(12)
    st = sim.status
    assert st.commanded["transfer_pump_1"] is True, "the controller commands the pump"
    assert asdict(sim.plant.outputs) == untouched, "a command reached the plant"
    assert writer.last_frame.transfer_pump_1 is True
    assert st.outputs_enabled is False and st.write_ok is False and st.written is None
    assert st.output_write == "NOT_WRITTEN_OUTPUTS_DISABLED"
    assert not sim.alarm("OUTPUT_WRITE_FAILED"), "not a write failure: outputs are disabled"
    kinds = {r["kind"] for r in sim.history.events}
    assert "OUTPUT_WRITTEN" not in kinds and "OUTPUT_WRITE_FAILED" not in kinds
    assert {"OUTPUT_REQUESTED", "OUTPUT_NOT_WRITTEN"} <= kinds
    not_written = [r for r in sim.history.events if r["kind"] == "OUTPUT_NOT_WRITTEN"]
    assert all(r["data"]["reason"] == "OUTPUTS_DISABLED" for r in not_written)
    assert any(r["data"]["changes"].get("transfer_pump_1") for r in not_written)
    start = next(r for r in sim.history.events if r["kind"] == "CONTROLLER_START"
                 and r["data"].get("outputs_enabled") is False)
    assert start["data"]["data_source"] == "SIMULATED"
    samples = [r for r in sim.history.samples if r["data"].get("outputs_enabled") is False]
    assert samples and all(r["data"]["written"] is None for r in samples)
    # 4. Supervision is kept: the pump has no flow, so the no-flow fault latches.
    assert sim.run(30, until=lambda s: s.alarm("TRANSFER_1_NO_FLOW") is not None)
    # 5. Shutdown: the safe frame is never reported as written.
    result = sim.ctl.shutdown("test")
    assert result.safe_frame_written is False and result.outputs_enabled is False
    assert asdict(sim.plant.outputs) == untouched
    stop = [r for r in sim.history.events if r["kind"] == "CONTROLLER_STOP"][-1]
    assert stop["data"]["safe_frame_written"] is False
    assert stop["data"]["outputs_enabled"] is False
    final = [r for r in sim.history.events if r["data"].get("cause") == "SHUTDOWN"]
    assert {r["kind"] for r in final} == {"OUTPUT_REQUESTED", "OUTPUT_NOT_WRITTEN"}


@scenario(83, "Hardware reader in shadow run: monitoring mode, reads only, unused input not resolved")
def s83():
    sys.path.insert(0, str(ROOT / "tools"))
    import run_controller
    from pasteurizer.hardware import HardwareReader
    fake, restore = load_fake_revpi()
    try:
        config = load_config()
        reader = HardwareReader(mapping=run_controller.reader_mapping(config))
        kwargs = fake.RevPiModIO.instances[0].kwargs
        assert kwargs["monitoring"] is True and kwargs["autorefresh"] is False
        for _ in range(5):
            snapshot = reader.read()
        reader.close()
        assert fake.WRITES == [], fake.WRITES
        assert "I_10_i06" not in fake.RESOLVED, "unused level input resolved"
        assert "level_switch_10" not in snapshot["digital_inputs"]
        assert snapshot["digital_inputs"]["auto_switch"] is True
        assert "write" not in dir(HardwareReader) and "writeprocimg" not in (
            ROOT / "pasteurizer" / "hardware.py").read_text(encoding="utf-8")
    finally:
        restore()


@scenario(84, "Level switch 10 unused: disabled, no signal, no GUI tag; an unused enabled switch is refused")
def s84():
    config = load_config()
    assert config.levels["level_switch_10"].enabled is False
    assert "level_switch_10" not in config.level_roles.values()
    processor = SignalProcessor(config)
    assert "level_switch_10" not in processor.levels
    assert set(processor.levels) == {f"level_switch_{n}" for n in range(1, 10)}
    assert "level_switch_10" not in BY_ID
    sim = Sim(params=FAST, bath=84.0)
    sim.run(1)
    assert "level_switch_10" not in sim.status.signals["levels"]
    folder = Path(tempfile.mkdtemp()) / "config"
    shutil.copytree(ROOT / "config", folder)
    levels = json.loads((folder / "level_settings.json").read_text(encoding="utf-8"))
    levels["level_switch_10"]["enabled"] = True
    (folder / "level_settings.json").write_text(json.dumps(levels), encoding="utf-8")
    try:
        load_config(folder)
        raise AssertionError("an enabled level switch without a role was accepted")
    except ConfigError as error:
        assert "level_switch_10" in str(error)


@scenario(85, "Shadow runner on fake RevPi I/O: LIVE inputs, nothing written, GUI and log say so")
def s85():
    folder = Path(tempfile.mkdtemp())
    database, state, lock = folder / "shadow.sqlite", folder / "state.json", folder / "run.lock"
    write_log = folder / "writes.txt"
    port = free_port()
    env = dict(os.environ, PYTHONPATH=str(fake_revpi_dir()), FAKE_REVPI_WRITE_LOG=str(write_log))
    command = [sys.executable, str(ROOT / "tools" / "run_controller.py"), "--outputs", "disabled",
               "--minutes", "0.12", "--port", str(port), "--database", str(database),
               "--state", str(state), "--lock", str(lock), "--token", str(folder / "token"),
               "--command-port", str(free_port())]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, cwd=ROOT, env=env)
    status = None
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            code, body = request(port, "/status")
        except OSError:
            continue
        if code == 200 and body["cycle"] > 15:
            status = body
            break
    # A second runner while the first holds the lock is refused.
    second = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, env=env, timeout=30)
    output = process.communicate(timeout=30)[0]
    assert process.returncode == 0, output
    assert second.returncode != 0 and "Another controller is running" in second.stderr + second.stdout
    assert not write_log.exists(), write_log.read_text(encoding="utf-8")          # no write attempt at all
    assert "OUTPUTS DISABLED / SHADOW RUN" in output
    assert "safe frame NOT written (outputs disabled)" in output, output
    assert status is not None, output
    assert status["data_source"] == "LIVE" and status["mode"] == "AUTO"
    assert status["outputs_enabled"] is False and status["output_write"] == "NOT_WRITTEN_OUTPUTS_DISABLED"
    assert status["written"] is None and status["write_ok"] is False
    assert status["commanded"] and status["requested"]
    assert "level_switch_10" not in status["signals"]["levels"]
    for name in ("flow_sensor_1", "flow_sensor_2", "flow_sensor_3"):
        flow = status["signals"]["flows"][name]
        assert flow["flow_lpm"] is None and flow["calibrated"] is False, flow
        assert isinstance(flow["raw_counter"], int)
    assert abs(status["signals"]["temperatures"]["PT3_B4"]["celsius"] - 25.0) < 1e-9
    # The history and the GUI API: commands and "not written" kept apart.
    gui = GuiServer(ControllerLink("http://127.0.0.1:9"), database, port=0)
    gui.start()
    try:
        rows = request(gui.port, "/api/events?limit=500")[1]["rows"]
        kinds = {r["kind"] for r in rows}
        assert "OUTPUT_WRITTEN" not in kinds and "OUTPUT_WRITE_FAILED" not in kinds
        assert {"OUTPUT_REQUESTED", "OUTPUT_NOT_WRITTEN"} <= kinds
        for r in rows:
            if r["kind"] == "OUTPUT_NOT_WRITTEN":
                assert r["description"].startswith("NOT WRITTEN (outputs disabled, shadow run)")
            if r["kind"] == "OUTPUT_REQUESTED":
                assert r["description"].startswith("Requested")
            assert r["source"] == "LIVE"
        start = next(r for r in rows if r["kind"] == "CONTROLLER_START")
        assert "outputs DISABLED" in start["description"]
        stop = next(r for r in rows if r["kind"] == "CONTROLLER_STOP")
        assert "safe frame NOT written" in stop["description"]
        code, body, _ = request(gui.port, "/api/export.csv?kinds=OUTPUT_NOT_WRITTEN", raw=True)
        exported = list(csv.DictReader(io.StringIO(body)))
        assert code == 200 and exported
        assert all(r["description"].startswith("NOT WRITTEN") for r in exported)
    finally:
        gui.stop()


@scenario(86, "Shadow files separate; GUI pages label shadow runs and unwritten outputs")
def s86():
    # Shadow files never shared with the simulation or the outputs-enabled run, and back.
    refused = [("disabled", n) for n in ("simulation_state.json", "simulation.sqlite",
                                         "historian.sqlite", "controller.sqlite",
                                         "controller_state.json")]
    refused += [("enabled", n) for n in ("shadow.sqlite", "shadow_state.json",
                                             "simulation.sqlite")]
    for outputs, name in refused:
        flag = "--state" if name.endswith(".json") else "--database"
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "run_controller.py"),
                                 "--outputs", outputs, flag, str(ROOT / "logs" / name)],
                                capture_output=True, text=True, cwd=ROOT, timeout=30)
        assert result.returncode == 2 and "belongs to another run" in result.stderr, result.stderr
    from pasteurizer.run_lock import RunLock
    path = Path(tempfile.mkdtemp()) / "run.lock"
    first, second = RunLock(path), RunLock(path)
    try:
        assert first.acquire() and not second.acquire()
        first.release()
        assert second.acquire()
    finally:
        first.release()
        second.release()
    # Event descriptions and page texts.
    severity, text, equipment, _ = describe(
        "OUTPUT_NOT_WRITTEN", {"changes": {"heater_3kw": True}, "reason": "OUTPUTS_DISABLED"})
    assert text == "NOT WRITTEN (outputs disabled, shadow run); command: heater_3kw=ON"
    assert equipment == ["heater_3kw"] and severity == "INFO"
    assert describe("OUTPUT_REQUESTED", {"changes": {"heater_3kw": True}})[1] == \
        "Requested: heater_3kw=ON"
    static = ROOT / "gui" / "static"
    app = (static / "app.js").read_text(encoding="utf-8")
    assert SHADOW_TEXT in app and NOT_WRITTEN in app
    assert SHADOW_TEXT in (static / "index.html").read_text(encoding="utf-8")
    assert SHADOW_TEXT in (static / "log.js").read_text(encoding="utf-8")


@scenario(87, "Live timing: a snapshot stamped after the cycle start is fresh, not stale")
def s87():
    sim = Sim(params=FAST, bath=84.0)
    plant = sim.plant

    class LaterStampReader:
        """Like a real reader: the clock moves on during the read (1 ms)."""
        data_source = "SIMULATED"

        def read(self):
            sim.clock.advance(0.001)
            return plant.read()

    sim.ctl = PlantController(sim.config, reader=LaterStampReader(), writer=plant,
                              store=sim.store, event_log=sim.log, clock=sim.clock,
                              simulated_io=True, heartbeat=sim.watchdog.heartbeat)
    sim.run(5)
    acquisition = sim.status.signals["acquisition"]
    assert acquisition["valid"] is True and acquisition["age_seconds"] >= 0, acquisition
    assert sim.status.mode == "AUTO" and sim.state != "TRIPPED"
    assert not sim.alarm("ACQUISITION_UNAVAILABLE") and not sim.alarm("ACQUISITION_LOST")
