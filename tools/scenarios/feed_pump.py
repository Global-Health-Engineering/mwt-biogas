"""Effluent feed pump GPST-PU-02: sequence, run-on, trips, protections.

Start (AUTO): Y3 open -> after its travel time the pump -> Y4. Normal stop:
Y3/Y4 close, the pump runs on briefly. Trips: pump off with the valves.
Closed-valve protection (AUTO and MANUAL), dry-run protection (buffer tank
low) and the Y4 minimum opening. All plant numbers are PRETEND values.
"""

from pasteurizer.config import ConfigError
from pasteurizer.control_settings import ControlSettingsError
from pasteurizer.modes import Mode
from scenarios.harness import (
    COMMISSIONING,
    FAST,
    Sim,
    make_config,
    overtemperature_offset,
    scenario,
    state_is,
)

PUMP = "effluent_feed_pump"


def _until_out(name, value=True):
    return lambda sim: getattr(sim.plant.outputs, name) == value


def _to_release(sim):
    assert sim.run(1200, until=state_is("FALLBACK_TREATMENT")), sim.status.state_reason
    assert sim.run(20, until=lambda s: s.out("control_valve_percent") > 0)


@scenario(147, "Feed pump start: Y3 written open, pump 10 s later, Y4 2 s after the pump; flow only then")
def s147():
    sim = Sim(params=FAST, bath=84.0)
    assert sim.run(1200, until=state_is("FALLBACK_TREATMENT"))
    assert sim.out("pasteurizer_isolation_open"), "Y3 written open with the state change"
    t_y3 = sim.clock.monotonic()
    assert not sim.out(PUMP) and sim.out("control_valve_percent") == 0.0
    assert sim.run(15, until=_until_out(PUMP))
    t_pump = sim.clock.monotonic()
    assert 9.9 <= t_pump - t_y3 <= 10.5, t_pump - t_y3
    assert sim.out("control_valve_percent") == 0.0
    assert sim.plant.treatment_flow_l_min == 0.0
    assert sim.run(5, until=lambda s: s.out("control_valve_percent") > 0)
    assert 1.9 <= sim.clock.monotonic() - t_pump <= 2.5, sim.clock.monotonic() - t_pump
    sim.run(2)
    assert sim.plant.treatment_flow_l_min > 0
    assert not sim.alarm("FEED_PUMP_CLOSED_VALVES"), "normal start stays inside the limit"
    # The simulator gives no feed flow without the pump.
    sim.plant.outputs = sim.plant.outputs.__class__(
        **{**sim.plant.outputs.__dict__, PUMP: False})
    assert sim.plant.treatment_flow_l_min == 0.0


@scenario(148, "Normal stop (AUTO -> OFF): Y3/Y4 close in one frame, the feed pump runs on 2 s, then off")
def s148():
    sim = Sim(params=FAST, bath=84.0)
    _to_release(sim)
    sim.run(5)
    assert sim.out(PUMP)
    sim.plant.mode = Mode.OFF
    assert sim.run(2, until=lambda s: not s.out("pasteurizer_isolation_open"))
    t_closed = sim.clock.monotonic()
    assert sim.out("control_valve_percent") == 0.0
    assert sim.out(PUMP), "pump runs on while the valves close"
    assert sim.run(5, until=_until_out(PUMP, False))
    assert 1.9 <= sim.clock.monotonic() - t_closed <= 2.5, sim.clock.monotonic() - t_closed
    assert sim.run(60, until=state_is("IDLE"))
    assert not sim.alarm("FEED_PUMP_CLOSED_VALVES")


@scenario(149, "Trip during treatment: the feed pump goes off in the same frame as Y3/Y4 (no run-on)")
def s149():
    sim = Sim(params=FAST, bath=84.0)
    _to_release(sim)
    sim.run(5)
    assert sim.out(PUMP)
    sim.plant.faults.temperature_offsets["PT6_B7"] = overtemperature_offset(sim)
    assert sim.run(2, until=lambda s: not s.out("pasteurizer_isolation_open"))
    assert sim.state == "TRIPPED"
    assert not sim.out(PUMP) and sim.out("control_valve_percent") == 0.0


@scenario(150, "AUTO: Y4 stuck closed -> pump cut off after 10 s, warning, treatment interrupted; next start re-arms")
def s150():
    sim = Sim(params=FAST, bath=84.0)
    assert sim.run(1200, until=state_is("FALLBACK_TREATMENT"))
    sim.plant.faults.y4_stuck = True                  # feedback stays at 0 %
    assert sim.run(15, until=_until_out(PUMP))
    t_pump = sim.clock.monotonic()
    assert sim.run(15, until=_until_out(PUMP, False))
    assert 9.9 <= sim.clock.monotonic() - t_pump <= 10.5, sim.clock.monotonic() - t_pump
    assert sim.state == "FALLBACK_QUALIFY", sim.state
    assert "closed valves" in sim.status.state_reason
    assert sim.alarm("FEED_PUMP_CLOSED_VALVES")
    sim.run(1)
    assert not sim.out(PUMP) and not sim.out("pasteurizer_isolation_open")
    transitions = [e for e in sim.events("STATE_TRANSITION")
                   if e["data"]["new"] == "FALLBACK_QUALIFY" and e["data"]["interruption"]]
    assert transitions, "counted as an interruption"
    # Valve repaired: the next treatment start re-arms the pump and clears the warning.
    sim.plant.faults.y4_stuck = False
    assert sim.run(600, until=state_is("FALLBACK_TREATMENT"))
    assert sim.run(15, until=_until_out(PUMP))
    assert not sim.alarm("FEED_PUMP_CLOSED_VALVES")
    assert sim.run(5, until=lambda s: s.out("control_valve_percent") > 0)


@scenario(151, "MANUAL: pump against closed Y3 is cut off after 10 s and its request withdrawn; a new press runs it")
def s151():
    sim = Sim(params=FAST, bath=40.0)
    sim.plant.mode = Mode.MANUAL
    sim.run(11)          # start-up switching pause (outputs possibly on) is over
    assert sim.ctl.commands.manual_request(COMMISSIONING, PUMP, True).accepted
    assert sim.run(1, until=_until_out(PUMP)), "MANUAL does not force the Y3 -> pump order"
    t_on = sim.clock.monotonic()
    assert sim.run(15, until=_until_out(PUMP, False))
    assert 9.9 <= sim.clock.monotonic() - t_on <= 10.5, sim.clock.monotonic() - t_on
    assert sim.alarm("FEED_PUMP_CLOSED_VALVES")
    assert PUMP not in sim.ctl.manual.active_requests(), "request withdrawn"
    released = [e for e in sim.events("MANUAL_RELEASE_AUTOMATIC")
                if e["data"]["command"] == PUMP]
    assert released and "closed valves" in released[-1]["data"]["cause"]
    renewed = sim.ctl.commands.manual_request(COMMISSIONING, PUMP, True, renew=True)
    assert not renewed.accepted, "the page's renewal cannot restore it"
    sim.run(3)
    assert not sim.out(PUMP)
    # A new press with Y3 open and Y4 open: runs, no cutoff.
    sim.ctl.commands.manual_request(COMMISSIONING, "pasteurizer_isolation_open", True)
    sim.ctl.commands.manual_request(COMMISSIONING, "control_valve_percent", 20.0)
    sim.run(12)                                       # Y4 opens 10 s after Y3
    assert sim.ctl.commands.manual_request(COMMISSIONING, PUMP, True).accepted
    sim.run(1)
    assert sim.out(PUMP) and not sim.alarm("FEED_PUMP_CLOSED_VALVES")
    for _ in range(2):
        sim.run(10)
        for command, value in ((PUMP, True), ("pasteurizer_isolation_open", True),
                               ("control_valve_percent", 20.0)):
            sim.ctl.commands.manual_request(COMMISSIONING, command, value, renew=True)
    assert sim.out(PUMP), "open valves: no cutoff"
    assert sim.ctl.commands.manual_release(COMMISSIONING, PUMP).accepted
    sim.run(0.4)
    assert not sim.out(PUMP), "MANUAL release: off at once (valves still open, no run-on)"


@scenario(152, "Dry run: buffer tank low blocks the feed pump in MANUAL and AUTO; pump allowed again when it clears")
def s152():
    sim = Sim(params=FAST, bath=40.0)
    sim.plant.mode = Mode.MANUAL
    low = sim.config.level_roles["effluent_tank_low"]
    sim.plant.faults.forced_switches[low] = False      # below the low switch
    sim.run(11)          # start-up switching pause (outputs possibly on) is over
    sim.ctl.commands.manual_request(COMMISSIONING, "pasteurizer_isolation_open", True)
    assert sim.ctl.commands.manual_request(COMMISSIONING, PUMP, True).accepted
    sim.run(2)
    assert not sim.out(PUMP), "dry-run protection"
    notes = [n for n in sim.ctl._last_notes if n.output == PUMP]
    assert notes and "Buffer tank low" in notes[0].message, notes
    del sim.plant.faults.forced_switches[low]
    sim.run(1)
    assert sim.out(PUMP), "level back: the held request runs the pump"
    # AUTO: the treatment path stays isolated while the tank is low (supply).
    auto = Sim(params=FAST, bath=84.0)
    _to_release(auto)
    auto.plant.faults.forced_switches[low] = False
    assert auto.run(2, until=_until_out(PUMP, False))
    assert not auto.out("pasteurizer_isolation_open")


@scenario(153, "Y4 minimum opening: never below 5 % while treating; held there with PT3 low for 60 s stops treatment")
def s153():
    sim = Sim(params={**FAST, "y4_full_flow_l_min": 8.0}, bath=84.0)
    _to_release(sim)
    sim.run(3)
    sim.plant.faults.temperature_offsets["PT3_B4"] = -6.0   # reads ~78 °C: below target, above 65 °C
    positions = []
    stopped = False
    for _ in range(200):
        sim.run(1)
        if sim.state != "FALLBACK_TREATMENT":
            stopped = True
            break
        positions.append(sim.out("control_valve_percent"))
    assert stopped, positions[-5:]
    assert min(positions) == 5.0, min(positions)
    at_min = sum(1 for p in positions if p == 5.0)
    assert 59 <= at_min <= 66, at_min
    assert sim.state == "FALLBACK_QUALIFY" and "minimum opening" in sim.status.state_reason
    assert sim.alarm("Y4_AT_MINIMUM_OPENING")
    assert not sim.alarm("FEED_PUMP_CLOSED_VALVES")
    # Normal stop sequence: valves close, the pump runs on, then off.
    assert sim.out(PUMP) and not sim.out("pasteurizer_isolation_open")
    assert sim.run(5, until=_until_out(PUMP, False))


@scenario(154, "Feed pump settings: minimum opening above the closed tolerance; arbiter feed_pump keys required")
def s154():
    for value in (3.0, 2.0):
        try:
            make_config(control={"feed_pump.min_y4_opening_percent": value})
            raise AssertionError(f"minimum opening {value} % accepted")
        except (ControlSettingsError, ConfigError):
            pass
    try:
        make_config(control={"feed_pump.min_y4_opening_percent": 15.0})
        raise AssertionError("minimum above the fallback start opening accepted")
    except (ControlSettingsError, ConfigError):
        pass
    config = make_config(control={"feed_pump.min_y4_opening_percent": 8.0})
    assert config.control.feed_pump.min_y4_opening_percent == 8.0
    try:
        make_config(arbiter={"feed_pump": {"start_after_isolation_open_seconds": 10.0}})
        raise AssertionError("incomplete arbiter feed_pump accepted")
    except ConfigError:
        pass
