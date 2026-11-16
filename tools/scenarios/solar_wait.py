"""WAITING_FOR_SOLAR: a cold start with every check passed waits for solar
permission for the 3 kW heater; green double blink while it waits."""

import random

from gui import history_api
from pasteurizer.arbiter import IndicatorInputs, OutputArbiter
from pasteurizer.config import load_config
from pasteurizer.modes import Mode
from pasteurizer.process_state import ProcessState
from scenarios.harness import Sim, scenario


def _cold_night(**kwargs):
    sim = Sim(params=dict(bath_heat_capacity_j_k=2_000_000.0), bath=20.0, **kwargs)
    sim.solar.pv_w = 0.0
    return sim


@scenario(122, "WAITING_FOR_SOLAR: entered only after the start checks, transfer pumps run, left for preheat, STOP, OFF and warm water")
def s122():
    # Setup not ready (selector OFF): stays IDLE, never waits for solar.
    sim = _cold_night()
    sim.plant.mode = Mode.OFF
    sim.run(30)
    assert sim.status.state == "IDLE", sim.status.state
    assert sim.status.lamp_modes["green"] == "off"

    # Ready but dark: waits, nothing heats, logged with the solar reason.
    sim.plant.mode = Mode.AUTO
    sim.run(5)
    assert sim.status.state == "WAITING_FOR_SOLAR", sim.status.state
    entry = [e for e in sim.events("STATE_TRANSITION")
             if e["data"]["new"] == "WAITING_FOR_SOLAR"][-1]["data"]
    assert entry["old"] == "IDLE" and "not enough solar power" in entry["reason"]
    assert not any(sim.out(name) for name in ("heater_3kw", "heater_1kw"))
    indicators = sim.events("INDICATORS")[-1]["data"]
    assert indicators["process_state"] == "WAITING_FOR_SOLAR", indicators
    assert sim.status.lamp_modes == {"green": "double_blink", "yellow": "off", "red": "off"}
    text = history_api.describe("STATE_TRANSITION", entry)[1]
    assert "WAITING_FOR_SOLAR" in text, text

    # The transfer pumps (battery power) run while waiting: no 3 kW request,
    # so the preheat exclusion does not hold them off.
    sim.plant.tank[1] = 850.0
    assert sim.run(15, until=lambda s: s.out("transfer_pump_1"))
    assert not [n for n in sim.status.arbitration_notes if n["output"] == "transfer_pump_1"]
    assert sim.status.state == "WAITING_FOR_SOLAR"
    sim.plant.tank[1] = 50.0                        # below low: transfer ends
    assert sim.run(15, until=lambda s: not s.out("transfer_pump_1"))

    # Selector OFF while waiting: stops like any active state.
    sim.plant.mode = Mode.OFF
    sim.run(5)
    assert sim.status.state == "IDLE", sim.status.state
    sim.plant.mode = Mode.AUTO
    sim.run(5)
    assert sim.status.state == "WAITING_FOR_SOLAR"

    # Sun: the solar probe starts at once and the 3 kW preheats (enough PV
    # under load for the probe to pass: tools/scenarios/energy.py).
    sim.solar.pv_w = 4000.0
    assert sim.run(5, until=lambda s: s.status.state == "PREHEAT_3KW")
    sim.run(15)                                     # 10 s switching pause
    assert sim.out("heater_3kw") and sim.status.lamp_modes["green"] == "blinking"
    # Preheating: the pumps are excluded again while the 3 kW is requested.
    sim.plant.tank[1] = 850.0
    sim.run(10)
    assert not sim.out("transfer_pump_1")
    excluded = [n["rule"] for n in sim.status.arbitration_notes
                if n["output"] == "transfer_pump_1"] == ["PREHEAT_EXCLUSION"]
    assert excluded or not sim.status.requested["transfer_pump_1"]   # HEATER_3KW_ACTIVE
    sim.plant.tank[1] = 50.0

    # Water already warm while waiting (e.g. biogas heated it): a circulation
    # pulse refreshes PT5 first, then HEAT_UP (no electric heat).
    warm = _cold_night()
    warm.run(10)
    assert warm.status.state == "WAITING_FOR_SOLAR"
    warm.plant.bath_c = 70.0
    assert warm.run(60, until=lambda s: s.status.state == "HEAT_UP")
    assert [e["data"]["reason"] for e in warm.events("STATE_TRANSITION")][-1].startswith(
        "Preheat complete (solar wait): PT5"), warm.events("STATE_TRANSITION")[-1]
    assert not warm.out("heater_3kw")

    # Warm start at night needs no solar (biogas heating is never gated).
    night_warm = Sim(params=dict(bath_heat_capacity_j_k=2_000_000.0), bath=70.0)
    night_warm.solar.pv_w = 0.0
    night_warm.run(10)
    assert "WAITING_FOR_SOLAR" not in {e["data"]["new"]
                                       for e in night_warm.events("STATE_TRANSITION")}

    # Solar rule switched off: a cold start preheats at once.
    off = _cold_night(arbiter={"energy.solar_permission_enabled": False})
    off.run(10)
    assert off.status.state == "PREHEAT_3KW", off.status.state

    # GUI STOP while waiting.
    stop = _cold_night()
    stop.run(10)
    assert stop.status.state == "WAITING_FOR_SOLAR"
    stop.ctl.process.stop_hold = True
    stop.run(5)
    assert stop.status.state == "IDLE", stop.status.state


@scenario(123, "Green double blink: two short pulses then 2 s off; every pulse reaches the lamp at a 0.2 s scan")
def s123():
    config = load_config()
    settings = config.arbiter
    arbiter = OutputArbiter(config, start_time=0.0)
    pulse, pause = settings.solar_wait_pulse_seconds, settings.solar_wait_pause_seconds
    assert (pulse, pause) == (0.3, 2.0)
    period = 3 * pulse + pause
    # Exact shape: on, off, on, then the long pause.
    shape = [arbiter.double_blink_on(t / 100) for t in range(int(period * 100))]
    edges = [i for i in range(1, len(shape)) if shape[i] != shape[i - 1]]
    assert shape[0] and edges == [30, 60, 90], edges

    # Sampled once per 0.2 s scan with jitter and any phase: always exactly two
    # separate pulses per period, never merged or missed.
    rng = random.Random(4)
    for _ in range(200):
        t = rng.uniform(0, 100)
        samples = []
        for _ in range(200):
            t += 0.2 + rng.uniform(-0.03, 0.03)
            samples.append(arbiter.double_blink_on(t))
        rises = [i for i in range(1, len(samples)) if samples[i] and not samples[i - 1]]
        gaps = [b - a for a, b in zip(rises, rises[1:])]
        # Pairs: rises 0.6 s apart (2-4 scans), then about 2.3 s to the next pair.
        short = [g for g in gaps if g <= 4]
        long = [g for g in gaps if g >= 10]
        assert len(short) + len(long) == len(gaps), gaps
        assert abs(len(short) - len(long)) <= 1, gaps

    # The mode only in WAITING_FOR_SOLAR; a Level 3 still wins.
    modes = OutputArbiter.indicator_modes
    waiting = IndicatorInputs(process_state=ProcessState.WAITING_FOR_SOLAR,
                              warning=True, trip=False)
    assert modes(waiting) == {"green": "double_blink", "yellow": "steady", "red": "off"}
    tripped = IndicatorInputs(process_state=ProcessState.WAITING_FOR_SOLAR,
                              warning=False, trip=True)
    assert modes(tripped)["green"] == "off" and modes(tripped)["red"] == "steady"

    # In the written frames: the green output follows the pattern.
    sim = _cold_night()
    sim.run(12)
    assert sim.status.state == "WAITING_FOR_SOLAR"
    lamp = []
    for _ in range(int(3 * period / 0.2)):
        sim.run(0.2)
        lamp.append(sim.out("light_green"))
    rises = sum(1 for a, b in zip(lamp, lamp[1:]) if b and not a)
    assert 5 <= rises <= 7, lamp
    assert sum(lamp) <= len(lamp) * 0.4, "mostly off: a pause, not a blink"
