"""Shared harness for the controller scenarios.

Sim wires the real PlantController to the plant simulator, a fake clock,
an in-memory state store, an in-memory historian (drained between cycles,
as the writer thread would) and a software watchdog. All plant numbers are
PRETEND values.
"""

import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pasteurizer.clock import FakeClock
from pasteurizer.config import load_config
from pasteurizer.event_log import EventLog
from pasteurizer.historian import Historian, MemoryHistorianStore, SqliteStore
from pasteurizer.historian.sqlite_store import decode_sample, read_only_uri
from pasteurizer.model_interface import InputSource, ModelResult, ModelStatus
from pasteurizer.operator_interface import Role, User
from pasteurizer.controller import PlantController
from pasteurizer.persistence import MemoryStore
from pasteurizer.simulated_io import PlantParameters, SimulatedPlant
from pasteurizer.watchdog import SoftwareWatchdog

OPERATOR = User("op", Role.OPERATOR)
COMMISSIONING = User("comm", Role.COMMISSIONING)
CYCLE = 0.2

# Fast bath: ~1 K every 5 s at 3 kW (PRETEND, keeps scenarios short).
FAST = dict(bath_heat_capacity_j_k=16_000.0)


def make_config(control=None, arbiter=None, flow_confirmed=True):
    """Copy config/ with pretend flow factors and optional overrides.

    The simulation's factor (2000 pulses/L) is PRETEND and marked confirmed
    only in this temporary copy, so simulated flows are usable for control;
    config/flow_settings.json itself stays unassigned.
    """
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "config", tmp, dirs_exist_ok=True)
    flows = json.loads((tmp / "flow_settings.json").read_text())
    for options in flows.values():
        options["pulses_per_litre"] = 2000.0         # PRETEND for simulation
        options["calibration_confirmed"] = flow_confirmed
    (tmp / "flow_settings.json").write_text(json.dumps(flows))
    for name, changes in (("control_settings.json", control),
                          ("arbiter_settings.json", arbiter)):
        if changes:
            data = json.loads((tmp / name).read_text())
            for path, value in changes.items():
                node = data
                *parents, key = path.split(".")
                for part in parents:
                    node = node[part]
                node[key] = value
            (tmp / name).write_text(json.dumps(data))
    return load_config(tmp)


MODEL_ON = {
    "model_control_enabled": True,
    "flow_pi.kp_percent_per_l_min": 10.0,          # PRETEND tuning
    "flow_pi.ki_percent_per_l_min_s": 2.0,
    "flow_pi.max_flow_l_min": 1.5,
    "flow_pi.slew_percent_per_s": 2.0,
    "flow_pi.flow_rate_limit_l_min_per_s": 0.05,
}


class TestSqliteStore(SqliteStore):
    """Real SQLite store; `fail` simulates an unavailable disk."""

    fail = False
    delay_s = 0.0

    def write(self, records):
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise sqlite3.OperationalError("Simulated disk I/O error")
        super().write(records)


class Sim:
    def __init__(self, *, control=None, arbiter=None, params=None, store=None,
                 simulated_io=True, bath=None, battery=None, start_time=1000.0,
                 plant=None, clock=None, history=None, database=None, background=False,
                 flow_confirmed=True):
        self.config = make_config(control, arbiter, flow_confirmed)
        self.clock = clock or FakeClock(start_time)
        self.plant = plant or SimulatedPlant(self.config, self.clock,
                                             PlantParameters(**(params or {})))
        if bath is not None:
            self.plant.bath_c = bath
        self.store = store if store is not None else MemoryStore()
        self.wall_offset = 1.8e9
        self.clock_sync = None
        self.database = database
        if database is not None and history is None:
            history = TestSqliteStore(database, self.config.control.logging)
        self.history = history if history is not None else MemoryHistorianStore()
        self.historian = Historian(
            self.history, self.config.control.logging,
            # The writer thread runs on real time; without it, on simulated time.
            clock=time.monotonic if background else self.clock.monotonic,
            wall_clock=self.wall, clock_sync=lambda: self.clock_sync)
        if background:
            self.historian.start()
        self.log = EventLog(self.historian, clock=self.clock, wall_clock=self.wall,
                            clock_sync=lambda: self.clock_sync)
        self.battery = battery
        self.watchdog = SoftwareWatchdog(self.config.control.watchdog.heartbeat_timeout_s,
                                         self._watchdog_trip)
        self.ctl = PlantController(self.config, reader=self.plant, writer=self.plant,
                                   store=self.store, event_log=self.log, clock=self.clock,
                                   simulated_io=simulated_io,
                                   heartbeat=self.watchdog.heartbeat,
                                   battery_percent=lambda: self.battery)
        self.status = None
        self.model_sequence = 0
        self.model_template = None

    def wall(self):
        return self.wall_offset + self.clock.monotonic()

    def _watchdog_trip(self):
        self.plant.watchdog_shutdown()
        state = self.store.load()
        state.watchdog_tripped = True
        self.store.save(state)

    def restart(self, **kwargs):
        """New controller on the same plant, stores and clock (reboot)."""
        self.plant.powered = True
        if self.database is not None:
            kwargs.setdefault("database", self.database)
        else:
            kwargs.setdefault("history", self.history)
        sim = Sim(store=self.store, plant=self.plant, clock=self.clock, **kwargs)
        sim.wall_offset = self.wall_offset
        return sim

    def model(self, **changes):
        """Feed a fresh LIVE model result every cycle with these fields."""
        if changes:
            base = dict(status=ModelStatus.TRACKING, history_valid=True,
                        predicted_log_reduction=6.0, achievable_flow_l_min=0.8,
                        entry_target_met=True, predicted_pt3_c=80.0, residual_c=0.0)
            base.update(changes)
            self.model_template = base

    def run(self, seconds, until=None):
        for _ in range(max(1, round(seconds / CYCLE))):
            self.clock.advance(CYCLE)
            now = self.clock.monotonic()
            if self.model_template is not None:
                self.model_sequence += 1
                self.ctl.submit_model_result(ModelResult(
                    sequence=self.model_sequence, computed_at=now,
                    input_start=now - 1.0, input_end=now,
                    input_source=InputSource.LIVE, **self.model_template))
            self.status = self.ctl.cycle()
            self._drain()
            self.watchdog.check(now)
            if until is not None and until(self):
                return True
        return until is None

    def _drain(self):
        """Store queued records between cycles (the writer thread's job)."""
        if self.historian._background is None:
            self.historian.drain()

    def stall(self, seconds):
        """Controller hangs: time passes, no scan cycles; the watchdog still runs."""
        for _ in range(max(1, round(seconds / CYCLE))):
            self.clock.advance(CYCLE)
            self.watchdog.check(self.clock.monotonic())

    @property
    def state(self):
        return self.status.state

    def alarm(self, code):
        return next((a for a in self.status.alarms if a.code == code), None)

    def out(self, name):
        return self.plant.outputs.__getattribute__(name)

    def events(self, kind):
        """Stored events of a kind (lets the writer catch up first)."""
        self._drain()
        if self.database is not None:
            return [r for r in stored_rows(self.database, "events") if r["kind"] == kind]
        return [r for r in self.history.events if r["kind"] == kind]


def stored_rows(database, table) -> list[dict]:
    """Rows of a historian database table, data decoded."""
    db = sqlite3.connect(read_only_uri(database), uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id")]
    finally:
        db.close()
    for row in rows:
        if "data" in row:
            row["data"] = (decode_sample(row["data"]) if table == "samples"
                           else json.loads(row["data"]))
    return rows


def state_is(*states):
    return lambda sim: sim.state in states


def to_fallback_treatment(sim, timeout=1200):
    assert sim.run(timeout, until=state_is("FALLBACK_TREATMENT")), sim.status.state_reason
    sim.run(12)   # Y3 opening delay, Y4 released


SCENARIOS = {}


def scenario(number, title):
    def register(func):
        SCENARIOS[number] = (title, func)
        return func
    return register


# ---------------------------------------------------------------------------
