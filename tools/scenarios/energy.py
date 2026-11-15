"""Solar permission for the electric heaters (pasteurizer/energy.py, arbiter rule 4).

Thresholds, hysteresis, timers, SOC limits, no double counting, missing or
invalid inverter data, recovery, the arbiter, the controller with the plant
simulator, and the real launcher against a fake Modbus TCP inverter pair.
All plant numbers are PRETEND values.
"""

from dataclasses import replace
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading

from pasteurizer.arbiter import IndicatorInputs, OutputArbiter
from pasteurizer.clock import FakeClock
from pasteurizer.config import load_config
from pasteurizer.energy import (
    BLOCKED, DISABLED, PERMITTED, STARTING, STOPPING, EnergyManager, measurements,
)
from pasteurizer.output_commands import PERMISSIONED_COMMANDS, OutputCommands, OutputPermissions
from pasteurizer.process_state import ProcessState
from scenarios.harness import FAST, Sim, scenario
from scenarios.manual import (
    config_copy, get, output_channels, outputs, run_controller, wait_for,
)

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = load_config().arbiter.energy          # the repository's values (20/15 %, 120 s, 30/20 %)


def view(pv=3000, load=300, soc=80, *, soc2=None, fresh=(True, True), complete=None):
    """A SolarLink-shaped view: combined pv/load split over two inverters."""
    socs = (soc, soc if soc2 is None else soc2)
    inverters = [{"id": f"inverter_{n + 1}", "name": f"Inverter {n + 1}", "fresh": fresh[n],
                  "state": "OK" if fresh[n] else "OFFLINE",
                  "reading": {"battery_soc_pct": socs[n]}} for n in range(2)]
    return {"enabled": True, "inverters": inverters,
            "totals": {"pv_w": pv, "load_w": load, "battery_w": load - pv,
                       "complete": all(fresh) if complete is None else complete}}


class Rig:
    """EnergyManager on its own clock; step() advances and evaluates."""

    def __init__(self, **changes):
        self.settings = replace(SETTINGS, **changes)
        self.t = 0.0
        self.em = EnergyManager(self.settings, start_time=0.0)
        self.events = []
        self.status = None

    def step(self, seconds, data, on=()):
        """Evaluate every second for `seconds` with the same data."""
        for _ in range(max(1, int(round(seconds)))):
            self.t += 1.0
            self.status, events = self.em.update(data, {h: True for h in on}, self.t)
            self.events += events
        return self.status

    def mode(self, key):
        return self.status.modes[key]


@scenario(114, "Energy: start thresholds 200 / 600 / 800 W (20 %), SOC >= 30 %, reasons")
def s114():
    r = Rig(start_delay_s=0.0)
    # surplus = PV - load (no heater on)
    s = r.step(1, view(pv=500, load=301))                       # 199 W
    assert [s.modes[k].permitted for k in ("heater_1kw", "heater_3kw", "both")] == [False] * 3
    assert "199 W < 200 W (20% of 1 kW)" in s.modes["heater_1kw"].reason
    s = r.step(1, view(pv=500, load=300))                       # 200 W
    assert s.modes["heater_1kw"].permitted and not s.modes["heater_3kw"].permitted
    assert s.available_heating_w == 1000
    r = Rig(start_delay_s=0.0)
    s = r.step(1, view(pv=900, load=300))                       # 600 W
    assert s.modes["heater_3kw"].permitted and not s.modes["both"].permitted
    assert s.available_heating_w == 3000
    s = r.step(1, view(pv=1100, load=300))                      # 800 W
    assert s.modes["both"].permitted and s.available_heating_w == 4000
    assert s.modes["both"].start_need_w == 800 and s.modes["heater_3kw"].stop_need_w == 450
    # SOC: 29 % never starts, 30 % does.
    r = Rig(start_delay_s=0.0)
    s = r.step(1, view(pv=5000, load=0, soc=29))
    assert not any(m.permitted for m in s.modes.values())
    assert s.modes["heater_1kw"].reason == "Battery SOC 29 % < 30 %"
    s = r.step(1, view(pv=5000, load=0, soc=30))
    assert all(m.permitted for m in s.modes.values())
    # SOC of the two inverters: the lower counts.
    assert r.step(1, view(soc=70, soc2=65)).soc_percent == 65
    # Solar contribution: surplus as a share of the mode's power.
    assert r.step(1, view(pv=900, load=300)).modes["heater_3kw"].contribution_percent == 20.0


@scenario(115, "Energy: 120 s start delay, 120 s stop delay below 15 %, hysteresis band keeps running")
def s115():
    r = Rig()
    sunny = view(pv=1000, load=300)                             # 700 W
    s = r.step(60, sunny)
    assert s.modes["heater_3kw"].state == STARTING and not s.modes["heater_3kw"].permitted
    # the delay counts from the first sample with the condition met (t = 1 s)
    assert s.modes["heater_3kw"].timer_s == 61
    r.step(1, view(pv=800, load=300))                           # 500 W < 600: timer restarts
    assert r.mode("heater_3kw").state == BLOCKED
    s = r.step(120, sunny, on=())
    assert not s.modes["heater_3kw"].permitted and s.modes["heater_3kw"].timer_s == 1
    s = r.step(1, sunny)
    assert s.modes["heater_3kw"].permitted and s.modes["heater_3kw"].state == PERMITTED
    assert [e[1]["mode"] for e in r.events if e[1]["permitted"]] == ["heater_1kw", "heater_3kw"]
    # 3 kW on: the measured load includes it. Surplus 500 W: below the 600 W
    # start, above the 450 W stop -> keeps running (hysteresis).
    s = r.step(300, view(pv=800, load=3300), on=("heater_3kw",))
    assert s.surplus_w == 500 and s.modes["heater_3kw"].permitted
    # 400 W < 450 W: stop delay 120 s, a short recovery resets it.
    s = r.step(100, view(pv=700, load=3300), on=("heater_3kw",))
    assert s.modes["heater_3kw"].state == STOPPING and s.modes["heater_3kw"].timer_s == 21
    r.step(1, view(pv=800, load=3300), on=("heater_3kw",))
    assert r.mode("heater_3kw").state == PERMITTED
    s = r.step(120, view(pv=700, load=3300), on=("heater_3kw",))
    assert s.modes["heater_3kw"].permitted
    s = r.step(1, view(pv=700, load=3300), on=("heater_3kw",))
    assert not s.modes["heater_3kw"].permitted and "for 120 s: stopped" in s.modes["heater_3kw"].reason
    off = [e[1] for e in r.events if e[1]["mode"] == "heater_3kw" and not e[1]["permitted"]]
    assert off and off[-1]["surplus_w"] == 400


@scenario(116, "Energy: SOC <= 20 % stops at once; 20-30 % keeps running; restart needs 30 % and 120 s")
def s116():
    r = Rig()
    r.step(121, view(pv=2000, load=300, soc=50))
    assert r.mode("heater_1kw").permitted
    s = r.step(10, view(pv=2300, load=1300, soc=25), on=("heater_1kw",))
    assert s.modes["heater_1kw"].permitted, "between 20 and 30 % a running permission continues"
    s = r.step(1, view(pv=2300, load=1300, soc=20), on=("heater_1kw",))
    assert not s.modes["heater_1kw"].permitted
    assert s.modes["heater_1kw"].reason == "Battery SOC 20 % <= 20 %: 1 kW stopped"
    s = r.step(200, view(pv=2000, load=300, soc=29))
    assert not s.modes["heater_1kw"].permitted
    r.step(120, view(pv=2000, load=300, soc=30))
    assert not r.mode("heater_1kw").permitted
    r.step(1, view(pv=2000, load=300, soc=30))
    assert r.mode("heater_1kw").permitted


@scenario(117, "Energy: no double counting of heaters already in the measured load")
def s117():
    r = Rig(start_delay_s=0.0)
    # 1 kW running, measured load 1300 W includes it: other loads 300 W.
    s = r.step(1, view(pv=1100, load=1300), on=("heater_1kw",))
    assert s.other_loads_w == 300 and s.surplus_w == 800 and s.heater_on_w == 1000
    # Adding the 3 kW to the running 1 kW = both: needs 800 W beside other loads.
    assert s.modes["both"].permitted and s.available_heating_w == 4000
    s = r.step(1, view(pv=1099, load=1300), on=("heater_1kw",))
    assert s.surplus_w == 799
    # The same numbers with the heater NOT subtracted would show -200 W.
    s = r.step(1, view(pv=1100, load=1300), on=())
    assert s.surplus_w == -200 and s.other_loads_w == 1300
    # Unknown heater state (no written frame) counts as off: never a higher surplus.
    r2 = Rig(start_delay_s=0.0)
    s, _ = r2.em.update(view(pv=1100, load=1300), None, 1.0)
    assert s.surplus_w == -200 and s.heaters_on == ()
    # A heater whose thermostat cut out: load lower than its rating -> other loads >= 0.
    s = r.step(1, view(pv=3000, load=500), on=("heater_3kw",))
    assert s.other_loads_w == 0 and s.surplus_w == 3000


@scenario(118, "Energy: missing, stale or invalid inverter data; timeout; recovery needs a new start delay")
def s118():
    r = Rig()
    assert not measurements(None).valid
    assert not measurements(view(fresh=(True, False))).valid
    assert "Inverter 2 OFFLINE" in measurements(view(fresh=(True, False))).reason
    assert not measurements(view(complete=False)).valid
    assert not measurements(view(pv=-5)).valid
    assert not measurements(view(soc=101)).valid
    assert not measurements(view(pv=float("nan"))).valid
    assert not measurements({"enabled": False, "reason": "x"}).valid
    # No data from the start: no permission, reason given.
    s = r.step(200, None)
    assert not any(m.permitted for m in s.modes.values()) and not s.data_valid
    assert s.modes["heater_1kw"].reason.startswith("No valid inverter data")
    # Data during part of the start delay, then lost: the start delay begins again.
    r.step(100, view(pv=2000, load=300))
    r.step(5, view(fresh=(True, False)))
    s = r.step(100, view(pv=2000, load=300))
    assert not s.modes["heater_1kw"].permitted
    s = r.step(21, view(pv=2000, load=300))
    assert s.modes["heater_1kw"].permitted
    # Running: data lost -> permission kept for 60 s, then off.
    s = r.step(59, view(fresh=(False, True)), on=("heater_1kw",))
    assert s.modes["heater_1kw"].permitted and s.modes["heater_1kw"].state == STOPPING
    assert s.data_age_s == 59
    s = r.step(1, view(fresh=(False, True)), on=("heater_1kw",))
    assert not s.modes["heater_1kw"].permitted
    assert "Inverter data missing for 60 s" in s.modes["heater_1kw"].reason
    # Recovery: valid data again, new permission only after 120 s.
    r.step(120, view(pv=2000, load=300))
    assert not r.mode("heater_1kw").permitted
    r.step(1, view(pv=2000, load=300))
    assert r.mode("heater_1kw").permitted
    # A short dropout within the timeout does not switch off.
    s = r.step(30, None, on=("heater_1kw",))
    assert s.modes["heater_1kw"].permitted
    s = r.step(1, view(pv=2300, load=1300), on=("heater_1kw",))
    assert s.modes["heater_1kw"].state == PERMITTED
    # Rule switched off: every mode permitted, no events.
    r3 = Rig(solar_permission_enabled=False)
    s = r3.step(5, None)
    assert all(m.permitted and m.state == DISABLED for m in s.modes.values()) and not r3.events


@scenario(119, "Energy in the arbiter: per heater, larger permitted heater kept, biogas heating never gated")
def s119():
    config = load_config()
    clock = FakeClock(0.0)
    arbiter = OutputArbiter(config, start_time=0.0)
    em = EnergyManager(replace(config.arbiter.energy, start_delay_s=0.0), start_time=0.0)
    every = OutputPermissions.only(*PERMISSIONED_COMMANDS)

    def arbitrate(requested, energy, state=ProcessState.HEAT_UP, seconds=12.0):
        result = None
        for _ in range(int(seconds / 0.2)):
            clock.advance(0.2)
            result = arbiter.arbitrate(requested, every, IndicatorInputs(state, False, False),
                                       clock.monotonic(), energy=energy)
            arbiter.record_write(result.commands, clock.monotonic(), True)
        return result

    night, _ = em.update(view(pv=0, load=300), {}, 1.0)
    second = OutputCommands(heater_1kw=True, heating_circulation=True)
    result = arbitrate(second, night)
    assert not result.commands.heater_1kw and result.commands.heating_circulation
    note = result.blocked("heater_1kw")[0]
    assert note.rule == "ENERGY_SOLAR" and "Solar surplus -300 W < 200 W" in note.message
    assert not arbitrate(second, None).commands.heater_1kw
    assert arbitrate(second, None).blocked("heater_1kw")[0].rule == "ENERGY_UNKNOWN"
    sun, _ = em.update(view(pv=700, load=300), {}, 2.0)          # 400 W: 1 kW only
    assert arbitrate(second, sun).commands.heater_1kw
    # Both requested (only possible without the preheat exclusion): keep the
    # larger permitted heater, drop the other with the reason.
    no_exclusion = replace(config, arbiter=replace(config.arbiter, preheat_excluded_outputs=()))
    arbiter = OutputArbiter(no_exclusion, start_time=clock.monotonic())
    both = OutputCommands(heater_1kw=True, heater_3kw=True)
    mid, _ = em.update(view(pv=1000, load=300), {}, 3.0)          # 700 W: 3 kW yes, both no
    result = arbitrate(both, mid, ProcessState.PREHEAT_3KW)
    assert result.commands.heater_3kw and not result.commands.heater_1kw
    assert "1 kW + 3 kW not permitted" in result.blocked("heater_1kw")[0].message
    full, _ = em.update(view(pv=1200, load=300), {}, 4.0)         # 900 W: both
    result = arbitrate(both, full, ProcessState.PREHEAT_3KW)
    assert result.commands.heater_3kw and result.commands.heater_1kw
    # Starting: the note carries the remaining start delay.
    em2 = EnergyManager(config.arbiter.energy, start_time=0.0)
    starting, _ = em2.update(view(pv=2000, load=300), {}, 1.0)
    note = arbitrate(OutputCommands(heater_1kw=True), starting).blocked("heater_1kw")[0]
    assert note.remaining_s == 120 and "permitted in 120 s" in note.message


@scenario(120, "Energy in the controller: night start waits without tripping, sun heats, clouds stop, events, 5 h counts heating only")
def s120():
    # Long preheat limit counted as heating time; PRETEND 300 s for the check.
    sim = Sim(params=dict(bath_heat_capacity_j_k=2_000_000.0), bath=20.0,
              control={"preheat.max_duration_s": 300.0},
              arbiter={"energy.start_delay_s": 120.0, "energy.stop_delay_s": 120.0})
    sim.solar.pv_w = 0.0                                        # night (AUTO is the default)
    sim.run(25)
    assert sim.status.state == "PREHEAT_3KW", sim.status.state
    assert sim.status.requested["heater_3kw"] and not sim.out("heater_3kw")
    note = [n for n in sim.status.arbitration_notes if n["output"] == "heater_3kw"][0]
    assert note["rule"] == "ENERGY_SOLAR" and "< 600 W" in note["message"]
    # A night far longer than the preheat limit: no trip (heater never on).
    sim.run(900)
    assert sim.status.state == "PREHEAT_3KW" and not sim.alarm("PREHEAT_TIMEOUT")
    energy = sim.status.energy
    assert energy["status"]["available_heating_w"] == 0 and energy["solar"]["inverters"]
    assert energy["settings"]["preheat_heating_s"] == 0.0
    # Sunrise: 600 W surplus held for 120 s, then the 3 kW runs.
    sim.solar.pv_w = 1000.0                                     # 700 W surplus
    sim.run(100)
    assert not sim.out("heater_3kw")
    sim.run(25)
    assert sim.out("heater_3kw")
    # With the heater on, the measured load includes its 3 kW: no self-blocking.
    assert sim.status.energy["status"]["surplus_w"] == 700
    granted = [e["data"] for e in sim.events("ENERGY_PERMISSION") if e["data"]["permitted"]]
    assert {g["mode"] for g in granted} == {"heater_1kw", "heater_3kw"}
    # Clouds: 400 W < 450 W for 120 s -> off.
    sim.solar.pv_w = 700.0
    sim.run(100)
    assert sim.out("heater_3kw")
    sim.run(25)
    assert not sim.out("heater_3kw")
    assert not [e for e in sim.events("ENERGY_PERMISSION")
                if e["data"]["mode"] == "heater_3kw"][-1]["data"]["permitted"]
    # Heating time counts only while the 3 kW was written on: about 120 s so far.
    heated = sim.ctl.process.preheat_heated_s
    assert 100 < heated < 140, heated
    # The preheat timeout still trips after 300 s of real heating.
    sim.solar.pv_w = 3000.0
    sim.run(500, until=lambda s: s.alarm("PREHEAT_TIMEOUT"))
    assert sim.alarm("PREHEAT_TIMEOUT")
    assert sim.ctl.process.preheat_heated_s > 300
    # The biogas heating is never gated: second stage with the sun gone.
    sim2 = Sim(params=FAST, bath=78.0)
    sim2.solar.pv_w = 0.0
    sim2.run(15)
    assert sim2.out("heating_circulation") and not sim2.out("heater_1kw")


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
    config_copy(folder, {"energy.start_delay_s": 3.0},
                {"poll_s": 1.0, "timeout_s": 0.5, "stale_after_s": 3.0,
                 "inverters": [{"id": f"inverter_{n + 1}", "name": f"Inverter {n + 1}",
                                "host": "127.0.0.1", "port": inv.port, "device_id": 1}
                               for n, inv in enumerate(inverters)]})
    process, port = run_controller(folder, {"I_3": False, "I_4": True, "I_8_i06": True}, 0.7)
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
        # Both inverters gone: the 3 kW stays on during the communication
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
