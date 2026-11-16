"""Solar permission for the electric heaters (pasteurizer/energy.py, arbiter rule 4).

Single heater: the probe (switch on as a test, measure the solar power that
comes in under load). Start conditions, per-inverter fresh readings after
the settle time, pass/fail limits, retry delay, deadlines, stale data,
lost request, interlock, sustained low contribution / high discharge, the
SOC cutoff, battery sign and summing (with the balance diagnostic), the
arbiter, AUTO WAITING_FOR_SOLAR <-> PREHEAT_3KW, MANUAL, curtailed PV in the
plant simulator, and the real launcher against fake Modbus inverters.

The unit scenarios fix their thresholds (PROBE below) so they do not depend
on the site tuning in config/arbiter_settings.json. PRETEND plant values.
"""

from dataclasses import replace
import json
import math
from pathlib import Path
import socket
import struct
import tempfile
import threading

from pasteurizer.arbiter import IndicatorInputs, OutputArbiter
from pasteurizer.clock import FakeClock
from pasteurizer.config import load_config
from pasteurizer.controller.recorder import energy_sample
from pasteurizer.energy import (
    ARMED, BLOCKED, DISABLED, IDLE, MEASURING, PERMITTED, PROBING, RETRY, RETRY_WAIT,
    RUNNING, SETTLING, STARTING, STOPPING, EnergyManager, measurements,
)
from pasteurizer.modes import Mode
from pasteurizer.output_commands import PERMISSIONED_COMMANDS, OutputCommands, OutputPermissions
from pasteurizer.process_state import ProcessState
from pasteurizer.solar import decode
from scenarios.harness import COMMISSIONING, FAST, Sim, scenario
from scenarios.manual import (
    config_copy, get, output_channels, outputs, run_controller, wait_for,
)

ROOT = Path(__file__).resolve().parents[2]
# Fixed thresholds for the unit scenarios (the agreed values).
PROBE = dict(solar_permission_enabled=True, soc_start_min_percent=70.0,
             soc_stop_at_percent=50.0, communication_timeout_s=60.0,
             start_surplus_fraction=0.20, stop_surplus_fraction=0.15,
             start_delay_s=120.0, stop_delay_s=120.0,
             probe_min_pv_w=50.0, probe_settle_s=10.0, probe_switch_on_timeout_s=30.0,
             probe_measure_timeout_s=30.0, probe_min_solar_contribution=0.80,
             probe_max_battery_discharge_w=853.0, probe_hold_s=120.0,
             probe_retry_delay_s=600.0, battery_balance_tolerance_w=300.0,
             battery_balance_tolerance_fraction=0.25)
SETTINGS = replace(load_config().arbiter.energy, **PROBE)
# Controller scenarios: the same values over whatever the site file holds.
SIM_ENERGY = {f"energy.{k}": v for k, v in PROBE.items()}


def view(pv=3000, load=300, soc=80, *, battery=None, soc2=None, fresh=(True, True),
         complete=None, read_at=(None, None)):
    """A SolarLink-shaped view: combined pv/load/battery split over two
    inverters (battery default: load - pv, + = discharging)."""
    battery = load - pv if battery is None else battery
    socs = (soc, soc if soc2 is None else soc2)
    inverters = [{"id": f"inverter_{n + 1}", "name": f"Inverter {n + 1}", "fresh": fresh[n],
                  "state": "OK" if fresh[n] else "OFFLINE", "read_at": read_at[n],
                  "reading": {"battery_soc_pct": socs[n], "battery_power_w": battery / 2}}
                 for n in range(2)]
    return {"enabled": True, "inverters": inverters,
            "totals": {"pv_w": pv, "load_w": load, "battery_w": battery,
                       "complete": all(fresh) if complete is None else complete}}


class Rig:
    """EnergyManager on its own 1 s clock. Readings are taken every
    `poll` seconds per inverter (inverter 2 offset), like SolarLink."""

    def __init__(self, poll=5.0, **changes):
        self.settings = replace(SETTINGS, **changes)
        self.t = 0.0
        self.poll = poll
        self.em = EnergyManager(self.settings, start_time=0.0)
        self.events = []
        self.status = None
        self.on = set()             # heaters in the last written frame
        self.requests = set()
        self.interlocks = {}
        self.follow = True          # heater written on whenever permitted and requested

    def read_times(self):
        """Last poll time per inverter: k x poll (+ poll / 2 for inverter 2)."""
        return tuple(math.floor((self.t - n * self.poll / 2) / self.poll) * self.poll
                     + n * self.poll / 2 for n in range(2))

    def step(self, seconds, data_fn, *, requests=None):
        """Every second for `seconds`: evaluate with data_fn(on) -> view kwargs."""
        if requests is not None:
            self.requests = set(requests)
        for _ in range(max(1, int(round(seconds)))):
            self.t += 1.0
            kwargs = data_fn(frozenset(self.on)) if callable(data_fn) else data_fn
            v = None if kwargs is None else view(**{"read_at": self.read_times(), **kwargs})
            self.status, events = self.em.update(
                v, {h: h in self.on for h in ("heater_1kw", "heater_3kw")}, self.t,
                requests={h: True for h in self.requests}, interlocks=dict(self.interlocks))
            self.events += events
            if self.follow:         # the arbiter writes requested heaters with permission
                self.on = {h for h in self.requests if self.status.modes[h].permitted
                           and h not in self.interlocks}
        return self.status

    def mode(self, key):
        return self.status.modes[key]

    def probe_events(self, event=None):
        return [d for k, d in self.events if k == "ENERGY_PROBE"
                and (event is None or d["event"] == event)]


def curtailed(pv_available=4000, base=300, accept=0, soc=80):
    """Full battery: PV follows the load (+ what the battery still accepts)."""
    def data(on):
        load = base + 1000 * ("heater_1kw" in on) + 3000 * ("heater_3kw" in on)
        pv = min(pv_available, load + accept)
        return {"pv": pv, "load": load, "soc": soc}
    return data


@scenario(114, "Probe start: heat request, both inverters current, SOC >= 70 %, PV >= 50 W (0 W: no test), no interlock")
def s114():
    r = Rig()
    s = r.step(1, curtailed())
    assert s.modes["heater_3kw"].state == BLOCKED and s.modes["heater_3kw"].reason == "No heat request"
    # Curtailed PV: measured surplus with the heater off is 0 W - the old
    # surplus rule could never start. The probe starts on PV >= 50 W.
    s = r.step(1, curtailed(), requests={"heater_3kw"})
    assert s.surplus_w == 0 and s.modes["heater_3kw"].state == PROBING
    assert s.modes["heater_3kw"].permitted and r.probe_events("START")
    # 0 W PV (night): the test is skipped, with the reason.
    r = Rig()
    s = r.step(5, {"pv": 0, "load": 300}, requests={"heater_1kw"})
    assert not s.modes["heater_1kw"].permitted
    assert s.modes["heater_1kw"].reason == "PV 0 W < 50 W: no probe" and not r.probe_events()
    s = r.step(1, {"pv": 49, "load": 300})
    assert not s.modes["heater_1kw"].permitted
    s = r.step(1, {"pv": 50, "load": 300})
    assert s.modes["heater_1kw"].phase in (ARMED, SETTLING)
    # SOC: 69 % no, 70 % yes.
    r = Rig()
    s = r.step(3, {"pv": 300, "load": 300, "soc": 69}, requests={"heater_1kw"})
    assert s.modes["heater_1kw"].reason == "Battery SOC 69 % < 70 %"
    assert s.modes["heater_1kw"].probe["start_blockers"] == ["Battery SOC 69 % < 70 %"]
    assert r.step(1, {"pv": 300, "load": 300, "soc": 70}).modes["heater_1kw"].permitted
    # Data: one inverter missing -> no probe.
    r = Rig()
    s = r.step(3, {"pv": 300, "load": 300, "fresh": (True, False)}, requests={"heater_1kw"})
    assert not s.modes["heater_1kw"].permitted and "Inverter 2 OFFLINE" in s.modes["heater_1kw"].reason
    # Interlock holding the heater off -> no probe.
    r = Rig()
    r.interlocks = {"heater_1kw": "PREHEAT_EXCLUSION: Not allowed while heater_3kw is requested"}
    s = r.step(3, {"pv": 300, "load": 300}, requests={"heater_1kw"})
    assert not s.modes["heater_1kw"].permitted and "PREHEAT_EXCLUSION" in s.modes["heater_1kw"].reason
    # Rule switched off: always permitted, no probe.
    r = Rig(solar_permission_enabled=False)
    s = r.step(3, None, requests={"heater_3kw"})
    assert s.modes["heater_3kw"].permitted and s.modes["heater_3kw"].state == DISABLED


@scenario(115, "Probe: settle 10 s after written on, then a fresh reading from EACH inverter; pass at 80 % / 853 W")
def s115():
    r = Rig(poll=5.0)
    r.follow = False
    s = r.step(1, curtailed(), requests={"heater_3kw"})
    assert s.modes["heater_3kw"].phase == ARMED
    r.step(3, curtailed())                      # switching pause: not written on yet
    assert r.mode("heater_3kw").phase == ARMED and r.mode("heater_3kw").permitted
    r.on = {"heater_3kw"}
    on_at = r.t + 1
    s = r.step(1, curtailed())
    assert s.modes["heater_3kw"].phase == SETTLING
    s = r.step(9, curtailed())
    assert s.modes["heater_3kw"].phase == SETTLING        # 9 s after on
    # From the settle point: inverter readings requested before it do not count.
    settle_point = on_at + 10
    seen = []
    while r.mode("heater_3kw").phase in (SETTLING, MEASURING):
        r.step(1, curtailed())
        seen.append((r.t, r.mode("heater_3kw").phase, r.read_times()))
        assert r.t < settle_point + 30
    passed = r.probe_events("PASS")
    assert passed, r.events
    used = passed[0]["read_at"]
    assert all(t >= settle_point for _, t in used), (used, settle_point)
    assert r.mode("heater_3kw").phase == RUNNING and r.mode("heater_3kw").state == PERMITTED
    # Curtailed PV under load: 3300 W PV, other loads 300 W -> 100 % solar.
    assert passed[0]["solar_contribution"] == 1.0 and passed[0]["battery_w"] == 0

    # Limits: exactly 80 % and 853 W pass; 79.9 % or 854 W fail.
    def probe_with(pv, load, battery, heater="heater_1kw"):
        rig = Rig()
        rig.step(30, {"pv": pv, "load": load, "battery": battery}, requests={heater})
        return rig.mode(heater), rig
    m, _ = probe_with(pv=1100, load=1300, battery=200)        # (1100 - 300) / 1000 = 80 %
    assert m.phase == RUNNING, m.reason
    m, rig = probe_with(pv=1099, load=1300, battery=201)
    assert m.state == RETRY_WAIT and "solar contribution 79.9 % (< 80 %)" in m.reason, m.reason
    m, _ = probe_with(pv=2000, load=1300, battery=853)          # charging-side PV, discharge at limit
    assert m.phase == RUNNING, m.reason
    m, _ = probe_with(pv=2000, load=1300, battery=854)
    assert m.state == RETRY_WAIT and "battery discharge 854 W (> 853 W)" in m.reason
    # Charging (negative battery power) is no discharge.
    m, _ = probe_with(pv=2500, load=1300, battery=-1200)
    assert m.phase == RUNNING


@scenario(116, "Probe ends: fail -> 10 min retry; deadlines; stale data -> retry; lost request / interlock -> no retry")
def s116():
    # Failed probe: off, 10 min, then again only with a request and PV.
    r = Rig()
    weak = curtailed(pv_available=1000)                     # under load only 1000 W PV
    r.step(30, weak, requests={"heater_3kw"})
    m = r.mode("heater_3kw")
    assert m.state == RETRY_WAIT and not m.permitted and not r.on
    assert r.probe_events("FAIL")[0]["solar_contribution"] < 0.8
    r.step(560, weak)
    assert r.mode("heater_3kw").state == RETRY_WAIT
    left = r.mode("heater_3kw").timer_s
    r.step(int(left) + 1, curtailed())                       # sun back, request still there
    assert r.mode("heater_3kw").phase in (ARMED, SETTLING) and len(r.probe_events("START")) == 2
    # After the delay without PV: no new probe.
    r = Rig()
    r.step(30, weak, requests={"heater_3kw"})
    r.step(601, {"pv": 0, "load": 300})
    assert r.mode("heater_3kw").state == BLOCKED and len(r.probe_events("START")) == 1
    # After the delay without a request: no new probe.
    r = Rig()
    r.step(30, weak, requests={"heater_3kw"})
    r.step(601, curtailed(), requests=set())
    assert r.mode("heater_3kw").reason == "No heat request" and len(r.probe_events("START")) == 1

    # Switch-on deadline (e.g. the output never written): retry delay.
    r = Rig()
    r.follow = False
    r.step(29, curtailed(), requests={"heater_3kw"})     # granted at t = 1 s
    assert r.mode("heater_3kw").phase == ARMED
    r.step(1, curtailed())                                # 29 s after the grant
    assert r.mode("heater_3kw").phase == ARMED
    r.step(1, curtailed())                                # 30 s: deadline
    assert r.mode("heater_3kw").state == RETRY_WAIT
    assert r.probe_events("SWITCH_ON_TIMEOUT")
    # Measure deadline: inverter 2 never reads again after the settle point.
    r = Rig()
    r.step(1, curtailed(), requests={"heater_1kw"})
    old = r.read_times
    r.read_times = lambda: (old()[0], 0.0)      # inverter 2: no new reading, still "fresh"
    r.step(45, curtailed())
    assert r.mode("heater_1kw").state == RETRY_WAIT
    ev = r.probe_events("MEASURE_TIMEOUT")
    assert ev and "Inverter 2" in ev[0]["reason"]
    # Stale data during the probe: off at once, retry delay.
    r = Rig()
    r.step(5, curtailed(), requests={"heater_1kw"})
    assert r.mode("heater_1kw").phase == SETTLING
    r.step(1, {"pv": 300, "load": 1300, "fresh": (True, False)})
    assert r.mode("heater_1kw").state == RETRY_WAIT and not r.mode("heater_1kw").permitted
    assert r.probe_events("STALE")
    # Lost heat request during the probe: ends, no retry delay.
    r = Rig()
    r.step(5, curtailed(), requests={"heater_1kw"})
    r.step(1, curtailed(), requests=set())
    assert r.mode("heater_1kw").phase == IDLE and r.probe_events("REQUEST_ENDED")
    r.step(1, curtailed(), requests={"heater_1kw"})
    assert r.mode("heater_1kw").phase in (ARMED, SETTLING)         # may probe again at once
    # An interlock during the probe: ends, no retry delay; restarts once it clears.
    r = Rig()
    r.step(5, curtailed(), requests={"heater_1kw"})
    r.interlocks = {"heater_1kw": "PERMISSION_DENIED: level"}
    r.step(1, curtailed())
    assert r.mode("heater_1kw").phase == IDLE and r.probe_events("INTERLOCK")
    r.interlocks = {}
    r.step(1, curtailed())
    assert r.mode("heater_1kw").phase in (ARMED, SETTLING)


@scenario(117, "Running: 120 s too low / too high -> off + retry; 60 s without data -> off; SOC <= 50 % off at once (also in a probe)")
def s117():
    r = Rig()
    r.step(30, curtailed(), requests={"heater_3kw"})
    assert r.mode("heater_3kw").phase == RUNNING
    # Background load rises: discharge 1000 W > 853 W.
    heavy = curtailed(pv_available=3300, base=1300)            # PV capped at 3300 W
    s = r.step(100, heavy)
    assert s.modes["heater_3kw"].state == STOPPING and s.modes["heater_3kw"].permitted
    assert "battery discharge 1000 W > 853 W" in s.modes["heater_3kw"].reason
    r.step(1, curtailed())                                     # recovers: timer reset
    assert r.mode("heater_3kw").state == PERMITTED
    r.step(120, heavy)               # held 119 s (counted from the first sample)
    assert r.mode("heater_3kw").permitted
    r.step(1, heavy)
    assert r.mode("heater_3kw").state == RETRY_WAIT and r.probe_events("HOLD_FAIL")
    assert not r.on
    # Contribution too low for 120 s (PV drops under load).
    r = Rig()
    r.step(30, curtailed(), requests={"heater_1kw"})
    cloud = curtailed(pv_available=900)                        # (900 - 300) / 1000 = 60 %
    r.step(121, cloud)
    assert r.mode("heater_1kw").state == RETRY_WAIT
    assert "solar contribution 60 % < 80 %" in r.probe_events("HOLD_FAIL")[0]["reason"]
    # Running, data lost: 60 s communication timeout, then retry delay.
    r = Rig()
    r.step(30, curtailed(), requests={"heater_1kw"})
    r.step(59, None)
    assert r.mode("heater_1kw").permitted and r.mode("heater_1kw").state == STOPPING
    r.step(1, None)
    assert r.mode("heater_1kw").state == RETRY_WAIT and r.probe_events("COMM_TIMEOUT")
    # SOC 50 % while running: off at once, no retry delay, restart needs 70 %.
    r = Rig()
    r.step(30, curtailed(), requests={"heater_1kw"})
    s = r.step(1, curtailed(soc=50))
    assert not s.modes["heater_1kw"].permitted and not r.on
    assert r.probe_events("SOC_CUTOFF")[0]["reason"] == "Battery SOC 50 % <= 50 %: 1 kW off at once"
    r.step(30, curtailed(soc=69))
    assert not r.mode("heater_1kw").permitted
    r.step(1, curtailed(soc=70))
    assert r.mode("heater_1kw").phase in (ARMED, SETTLING)
    # SOC 50 % during a probe (settling): off at once.
    r = Rig()
    r.step(4, curtailed(), requests={"heater_3kw"})
    assert r.mode("heater_3kw").phase == SETTLING
    r.step(1, curtailed(soc=50))
    assert r.mode("heater_3kw").phase == IDLE and not r.on and r.probe_events("SOC_CUTOFF")


@scenario(118, "Battery readings: signed register, summed over both inverters; balance residual logged, never decides")
def s118():
    # Register 190: two's complement, + = discharging.
    regs = [0] * 13
    regs[190 - 178] = (-1200) & 0xFFFF
    assert decode(regs)["battery_power_w"] == -1200
    regs[190 - 178] = 617
    assert decode(regs)["battery_power_w"] == 617
    # Measurements use the combined (summed) battery value; one inverter
    # missing -> no combined value at all.
    m = measurements(view(pv=500, load=1700, battery=1195))
    assert m.valid and m.battery_w == 1195 and m.battery_each == (597.5, 597.5)
    assert not measurements(view(pv=500, load=1700, fresh=(True, False))).valid
    # Balance residual PV + battery - load: about 0 when sign and sum are right.
    r = Rig()
    s = r.step(1, {"pv": 500, "load": 1700, "battery": 1200})
    assert s.balance_residual_w == 0 and s.balance_ok
    # An inverted sign shows as about twice the load: flagged, but the
    # decision is unchanged (diagnostic only).
    s = r.step(1, {"pv": 500, "load": 1700, "battery": -1200})
    assert s.balance_residual_w == -2400 and s.balance_ok is False
    ok, bad = Rig(), Rig()
    ok.step(30, {"pv": 2000, "load": 1300, "battery": -700}, requests={"heater_1kw"})
    bad.step(30, {"pv": 2000, "load": 1300, "battery": 700}, requests={"heater_1kw"})
    assert ok.mode("heater_1kw").phase == bad.mode("heater_1kw").phase == RUNNING
    flagged = bad.probe_events("PASS")[0]
    assert flagged["balance_ok"] is False and flagged["balance_residual_w"] == 1400
    # Tolerance: max(300 W, 25 % of load).
    assert Rig().step(1, {"pv": 0, "load": 2000, "battery": 2500}).balance_ok      # 500 <= 500
    assert not Rig().step(1, {"pv": 0, "load": 2000, "battery": 2501}).balance_ok
    # The residual goes into the periodic samples (site check).
    sim = Sim(params=FAST, bath=78.0, arbiter=SIM_ENERGY)
    sim.run(5)
    sample = energy_sample(sim.status)
    assert sample["balance_residual_w"] == 0 and sample["balance_ok"] is True
    assert set(sample["heaters"]) == {"heater_1kw", "heater_3kw"}


@scenario(119, "Arbiter: solar note with the retry countdown; both heaters keep the larger permitted one; biogas never gated")
def s119():
    config = load_config()
    config = replace(config, arbiter=replace(config.arbiter, energy=SETTINGS))
    clock = FakeClock(0.0)
    arbiter = OutputArbiter(config, start_time=0.0)
    every = OutputPermissions.only(*PERMISSIONED_COMMANDS)

    def arbitrate(requested, energy, state=ProcessState.HEAT_UP, seconds=12.0):
        result = None
        for _ in range(int(seconds / 0.2)):
            clock.advance(0.2)
            result = arbiter.arbitrate(requested, every, IndicatorInputs(state, False, False),
                                       clock.monotonic(), energy=energy)
            arbiter.record_write(result.commands, clock.monotonic(), True)
        return result

    r = Rig()
    r.step(30, curtailed(pv_available=900), requests={"heater_1kw"})   # 60 %: fail
    status = r.status
    second = OutputCommands(heater_1kw=True, heating_circulation=True)
    result = arbitrate(second, status)
    assert not result.commands.heater_1kw and result.commands.heating_circulation
    note = result.blocked("heater_1kw")[0]
    assert note.rule == "ENERGY_SOLAR" and "next probe in 9:" in note.message, note.message
    assert 580 < note.remaining_s <= 600
    assert arbitrate(second, None).blocked("heater_1kw")[0].rule == "ENERGY_UNKNOWN"
    # Night: no probe, biogas heating still runs.
    r = Rig()
    r.step(3, {"pv": 0, "load": 300}, requests={"heater_1kw"})
    result = arbitrate(second, r.status)
    assert not result.commands.heater_1kw and result.commands.heating_circulation
    assert "PV 0 W < 50 W: no probe" in result.blocked("heater_1kw")[0].message
    # Both requested (only without the preheat exclusion): surplus rule for
    # both, unchanged; the larger permitted single heater stays.
    no_exclusion = replace(config, arbiter=replace(config.arbiter, preheat_excluded_outputs=()))
    arbiter = OutputArbiter(no_exclusion, start_time=clock.monotonic())
    r = Rig()
    r.step(30, curtailed(), requests={"heater_3kw"})
    assert r.mode("heater_3kw").phase == RUNNING and not r.mode("both").permitted
    result = arbitrate(OutputCommands(heater_1kw=True, heater_3kw=True), r.status,
                       ProcessState.PREHEAT_3KW)
    assert result.commands.heater_3kw and not result.commands.heater_1kw
    assert "1 kW + 3 kW not permitted" in result.blocked("heater_1kw")[0].message


def _waiting_night():
    sim = Sim(params=dict(bath_heat_capacity_j_k=2_000_000.0), bath=20.0, arbiter=SIM_ENERGY)
    sim.solar.pv_w = 0.0
    sim.solar.charge_accept_w = 0.0            # full battery: PV follows the load
    return sim


@scenario(120, "AUTO: WAITING_FOR_SOLAR grants a probe before the heater is on; curtailed PV heats; failed probe waits 10 min; no trip")
def s120():
    sim = _waiting_night()
    sim.run(25)
    assert sim.status.state == "WAITING_FOR_SOLAR", sim.status.state
    assert "PV 0 W < 50 W: no probe" in sim.status.state_reason, sim.status.state_reason
    assert "PV 0 W < 50 W: no probe" in sim.status.heating_wait
    assert not sim.out("heater_3kw") and not [e for e in sim.events("ENERGY_PROBE")]
    # A night far longer than the preheat limit: no trip (heater never on).
    sim.run(900)
    assert sim.status.state == "WAITING_FOR_SOLAR" and not sim.alarm("PREHEAT_TIMEOUT")
    # Morning, battery full: PV curtailed to the background load (300 W), so
    # the surplus with the heater off is 0 W. The probe still starts:
    sim.solar.pv_w = 4000.0
    sim.run(0.4)
    probe = sim.events("ENERGY_PROBE")
    assert probe and probe[0]["data"]["event"] == "START", probe
    # Started on curtailed PV (no surplus with the heater off).
    assert probe[0]["data"]["surplus_w"] <= 0 and probe[0]["data"]["pv_w"] <= 300, probe[0]
    # Granted before the heater is on: WAITING_FOR_SOLAR -> PREHEAT_3KW.
    assert sim.status.state == "PREHEAT_3KW", sim.status.state
    entered = [e["data"] for e in sim.events("STATE_TRANSITION")
               if e["data"]["new"] == "PREHEAT_3KW"][-1]
    assert entered["old"] == "WAITING_FOR_SOLAR"
    assert sim.status.energy["status"]["modes"]["heater_3kw"]["phase"] in ("ARMED", "SETTLING")
    assert "Probe" in sim.status.heating_wait
    # Heater on, settle, fresh readings, pass: keeps heating.
    assert sim.run(40, until=lambda s: s.status.energy["status"]["modes"]["heater_3kw"]["phase"] == "RUNNING")
    passed = [e["data"] for e in sim.events("ENERGY_PROBE") if e["data"]["event"] == "PASS"][0]
    assert passed["pv_w"] >= 3000 and passed["solar_contribution"] >= 0.8 and passed["battery_w"] <= 853
    sim.run(60)
    assert sim.out("heater_3kw") and sim.status.state == "PREHEAT_3KW"
    assert sim.status.heating_wait == ""
    heated = sim.ctl.process.preheat_heated_s
    assert heated > 50, heated
    # Clouds: under load only 1000 W -> 23 % for 120 s -> off, back to waiting.
    sim.solar.pv_w = 1000.0
    assert sim.run(130, until=lambda s: s.status.state == "WAITING_FOR_SOLAR")
    assert not sim.out("heater_3kw")
    fail = [e["data"] for e in sim.events("ENERGY_PROBE") if e["data"]["event"] == "HOLD_FAIL"]
    assert fail and "solar contribution 23.3 % < 80 %" in fail[0]["reason"], fail
    # Retry delay respected: no probe for 10 min although PV is there.
    starts = len([e for e in sim.events("ENERGY_PROBE") if e["data"]["event"] == "START"])
    sim.run(590)
    assert sim.status.state == "WAITING_FOR_SOLAR" and not sim.out("heater_3kw")
    assert "next probe in" in sim.status.heating_wait, sim.status.heating_wait
    assert len([e for e in sim.events("ENERGY_PROBE") if e["data"]["event"] == "START"]) == starts
    # After the delay: probe again; with weak sun it fails again (FAIL) and waits.
    assert sim.run(30, until=lambda s: s.status.state == "PREHEAT_3KW")
    assert sim.run(40, until=lambda s: s.status.state == "WAITING_FOR_SOLAR")
    assert [e["data"]["event"] for e in sim.events("ENERGY_PROBE")][-1] == "FAIL"
    assert sim.ctl.process.preheat_heated_s > heated          # kept across the waits
    assert not sim.alarm("PREHEAT_TIMEOUT")


@scenario(124, "MANUAL: a 1 kW ON request is probed like AUTO; failed probe holds it off with the retry countdown")
def s124():
    sim = Sim(params=FAST, bath=25.0, arbiter=SIM_ENERGY)
    sim.solar.charge_accept_w = 0.0
    sim.plant.mode = Mode.MANUAL
    sim.run(2)

    def hold(seconds, command="heater_1kw"):
        for _ in range(int(seconds / 2)):
            sim.ctl.commands.manual_request(COMMISSIONING, command, True, renew=True)
            sim.run(2)
    assert sim.ctl.commands.manual_request(COMMISSIONING, "heater_1kw", True).accepted
    hold(4)
    mode = sim.status.energy["status"]["modes"]["heater_1kw"]
    assert mode["phase"] == "ARMED", mode        # granted; waits out the switching pause
    assert "1 kW heater: Probe: waiting for the 1 kW output" in sim.status.heating_wait
    hold(10)
    assert sim.out("heater_1kw")
    hold(30)
    assert sim.status.energy["status"]["modes"]["heater_1kw"]["phase"] == "RUNNING"
    assert sim.out("heater_1kw")
    # SOC 50 %: off with the first reading that shows it (inverters are read
    # every 5 s), although the operator still requests it.
    sim.solar.soc_percent = 50.0
    sim.ctl.commands.manual_request(COMMISSIONING, "heater_1kw", True, renew=True)
    assert sim.run(5.4, until=lambda s: not s.out("heater_1kw"))
    cut = [e["data"] for e in sim.events("ENERGY_PROBE") if e["data"]["event"] == "SOC_CUTOFF"]
    assert cut and cut[0]["soc_percent"] == 50.0
    sim.run(0.4)
    note = [n for n in sim.status.arbitration_notes if n["output"] == "heater_1kw"][0]
    assert note["rule"] == "ENERGY_SOLAR" and "SOC 50 % < 70 %" in note["message"], note
    # Weak sun: the probe fails, the request is held off for 10 min.
    sim.solar.soc_percent = 80.0
    sim.solar.pv_w = 600.0                                   # (600 - 300) / 1000 = 30 %
    hold(40)
    note = [n for n in sim.status.arbitration_notes if n["output"] == "heater_1kw"][0]
    assert "next probe in" in note["message"] and note["remaining_s"] > 500, note
    assert not sim.out("heater_1kw")
    assert "1 kW heater: Probe failed" in sim.status.heating_wait


@scenario(125, "Controller: sustained high battery discharge for 120 s switches the running heater off; retry after 10 min")
def s125():
    # With consistent readings (PV + battery = load), >= 80 % solar means at
    # most 20 % of the heater power from the battery. The discharge limit bites
    # when the battery also feeds what the load register does not show.
    sim = _waiting_night()
    sim.solar.pv_w = 4000.0
    assert sim.run(60, until=lambda s: s.status.energy["status"]["modes"]["heater_3kw"]["phase"] == "RUNNING")
    sim.solar.charge_accept_w = None
    sim.solar.pv_w = 3000.0                 # contribution (3000 - 300) / 3000 = 90 %
    sim.solar.unmetered_w = 600.0           # battery: 300 W + 600 W = 900 W > 853 W
    sim.run(100)
    assert sim.out("heater_3kw")
    mode = sim.status.energy["status"]["modes"]["heater_3kw"]
    assert mode["state"] == "STOPPING" and "battery discharge 900 W > 853 W" in mode["reason"], mode
    assert "solar contribution" not in mode["reason"].split(";")[0]
    assert sim.run(30, until=lambda s: not s.out("heater_3kw"))
    fail = [e["data"] for e in sim.events("ENERGY_PROBE") if e["data"]["event"] == "HOLD_FAIL"][-1]
    assert "battery discharge 900 W > 853 W for 120 s" in fail["reason"], fail
    assert fail["balance_residual_w"] == 600            # logged for the site check
    assert sim.status.state == "WAITING_FOR_SOLAR"
    assert sim.status.energy["status"]["modes"]["heater_3kw"]["state"] == "RETRY_WAIT"


@scenario(126, "Controller: stale inverter data during a probe ends it at once (heater off) and applies the retry delay")
def s126():
    sim = _waiting_night()
    sim.solar.pv_w = 4000.0
    assert sim.run(20, until=lambda s: s.out("heater_3kw"))
    sim.run(2)
    assert sim.status.energy["status"]["modes"]["heater_3kw"]["phase"] == "SETTLING"
    sim.solar.inverter_down = "inverter_2"
    sim.run(0.6)
    assert not sim.out("heater_3kw") and sim.status.state == "WAITING_FOR_SOLAR"
    stale = [e["data"] for e in sim.events("ENERGY_PROBE") if e["data"]["event"] == "STALE"]
    assert stale and "Inverter 2 OFFLINE" in stale[0]["reason"]
    sim.solar.inverter_down = None
    sim.run(60)
    assert not sim.out("heater_3kw")
    assert sim.status.energy["status"]["modes"]["heater_3kw"]["state"] == "RETRY_WAIT"


# --- Fake Modbus TCP inverter (no pymodbus needed for the server) -------------

class FakeInverter:
    """Answers function 03 (read holding registers) for registers 178..190."""

    def __init__(self, registers):
        self.registers = dict(registers)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.reads = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    while True:
                        header = conn.recv(7)
                        if len(header) < 7:
                            break
                        tid, _, length, unit = struct.unpack(">HHHB", header)
                        pdu = conn.recv(length - 1)
                        function, address, count = struct.unpack(">BHH", pdu[:5])
                        values = [self.registers.get(address + i, 0) & 0xFFFF for i in range(count)]
                        body = struct.pack(">BB", function, 2 * count) + struct.pack(f">{count}H", *values)
                        conn.sendall(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)
                        self.reads += 1
                except OSError:
                    pass

    def close(self):
        self.sock.close()


@scenario(121, "End to end: the launcher reads fake inverters over Modbus TCP; the 3 kW runs only with solar permission")
def s121():
    try:
        import pymodbus  # noqa: F401
        have_pymodbus = True
    except ImportError:
        have_pymodbus = False
    pv = {186: 600, 187: 600, 178: 150, 183: 5200, 184: 85, 190: 0}  # per inverter: 1.2 kW PV
    inverters = [FakeInverter(pv), FakeInverter(pv)]
    folder = Path(tempfile.mkdtemp())
    config_copy(folder, {f"energy.{k}": v for k, v in PROBE.items()},
                {"poll_s": 1.0, "timeout_s": 0.5, "stale_after_s": 3.0,
                 "inverters": [{"id": f"inverter_{n + 1}", "name": f"Inverter {n + 1}",
                                "host": "127.0.0.1", "port": inv.port, "device_id": 1}
                               for n, inv in enumerate(inverters)]})
    process, port = run_controller(folder, {"I_3": False, "I_4": True, "I_8_i06": True}, 1.0)
    channels = output_channels(load_config())
    try:
        if not have_pymodbus:
            status = wait_for(lambda: (lambda s: s if s["energy"] else None)(get(port, "/status")[1]), 20)
            assert status and not status["energy"]["status"]["data_valid"]
            assert "pymodbus" in json.dumps(status["energy"]["solar"])
            assert not status["commanded"]["heater_3kw"]
            print("    (pymodbus not installed here: checked that the 3 kW stays off)")
            return
        status = wait_for(lambda: (lambda s: s if s["state"] == "PREHEAT_3KW" else None)(
            get(port, "/status")[1]), 20)
        assert status, "AUTO cold start reaches PREHEAT_3KW"
        status = wait_for(lambda: (lambda s: s if s["written"] and s["written"]["heater_3kw"] else None)(
            get(port, "/status")[1]), 30)
        assert status, "3 kW written after the solar start delay"
        energy = status["energy"]["status"]
        assert energy["data_valid"] and energy["pv_w"] == 2400
        assert energy["modes"]["heater_3kw"]["permitted"]
        assert all(inv.reads > 0 for inv in inverters)
        assert wait_for(lambda: outputs(folder)[channels["heater_3kw"]]), "3 kW heater output switched"
        # Probe: 10 s settle, then one reading of each fake inverter after it:
        # 2400 W PV for 3 kW (the fake load register does not include the
        # heater, so other loads count 0 W) = 80 % -> passes.
        status = wait_for(lambda: (lambda s: s if s["energy"]["status"]["modes"]["heater_3kw"]
                                   ["phase"] == "RUNNING" else None)(get(port, "/status")[1]), 30)
        assert status, "probe passed with the fake inverters"
        # Both inverters gone: the running 3 kW stays on during the communication
        # timeout (60 s) - here only checked to still run a few seconds later.
        for inv in inverters:
            inv.close()
        status = wait_for(lambda: (lambda s: s if not s["energy"]["status"]["data_valid"] else None)(
            get(port, "/status")[1]), 15)
        assert status and status["energy"]["status"]["modes"]["heater_3kw"]["state"] == STOPPING
        assert status["commanded"]["heater_3kw"]
    finally:
        output = process.communicate(timeout=60)[0]
        for inv in inverters:
            inv.close()
    assert process.returncode == 0, output
