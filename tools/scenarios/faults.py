"""Fault, trip, reset and persistence scenarios."""

from pasteurizer.persistence import MemoryStore
from scenarios.harness import (
    COMMISSIONING,
    FAST,
    OPERATOR,
    Sim,
    scenario,
    state_is,
    to_fallback_treatment,
)


@scenario(11, "PT3 invalid 4 s recovers; 6 s trips")
def s11():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.plant.faults.invalid_temperatures.add("PT3_B4")
    sim.run(4)
    assert not sim.out("pasteurizer_isolation_open") and sim.out("heating_circulation")
    sim.plant.faults.invalid_temperatures.clear()
    assert sim.run(120, until=state_is("FALLBACK_TREATMENT"))
    sim.plant.faults.invalid_temperatures.add("PT3_B4")
    sim.run(6)
    assert sim.state == "TRIPPED" and sim.alarm("PT3_INVALID_PERSISTENT")


@scenario(12, "PT5 invalid: heating off at once; 8 s restarts, 12 s trips")
def s12():
    sim = Sim(params=FAST, bath=75.0)
    sim.run(30)
    sim.plant.faults.invalid_temperatures.add("PT5_B6")
    sim.run(0.4)
    assert not sim.out("heating_circulation") and not sim.out("heater_1kw")
    sim.run(7.6)
    sim.plant.faults.invalid_temperatures.clear()
    assert sim.run(20, until=state_is("HEAT_UP", "FALLBACK_QUALIFY"))
    sim.plant.faults.invalid_temperatures.add("PT5_B6")
    sim.run(12)
    assert sim.state == "TRIPPED"


@scenario(13, "Overtemperature: reset refused at 88 C, accepted at 84 C")
def s13():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(5)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 30.0
    sim.run(0.4)
    assert sim.state == "TRIPPED" and sim.out("light_red") and not sim.out("light_green")
    assert not sim.out("heating_circulation")
    sim.plant.faults.temperature_offsets["PT6_B7"] = 88.0 - sim.plant.bath_c
    sim.run(1)
    sim.ctl.commands.reset(OPERATOR)
    sim.run(0.4)
    assert sim.state == "TRIPPED"
    sim.plant.faults.temperature_offsets.clear()
    sim.plant.bath_c = 80.0
    sim.run(1)
    sim.ctl.commands.reset(OPERATOR)
    sim.run(0.2)
    assert sim.alarm("OVERTEMPERATURE") is None


@scenario(15, "Y4 stuck: Level 3 after 60 s")
def s15():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.run(5)
    sim.plant.faults.y4_stuck = True                 # stuck open at ~10 %
    sim.plant.faults.temperature_offsets["PT3_B4"] = -30.0   # forces closing
    sim.run(3)
    sim.plant.faults.temperature_offsets.clear()
    assert sim.run(70, until=state_is("TRIPPED"))
    assert sim.alarm("Y4_POSITION_TIMEOUT")
    assert not sim.out("pasteurizer_isolation_open") and not sim.out("heating_circulation")


@scenario(16, "One failed read recovers; 0.6 s trips plant with safe frame")
def s16():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(30)
    sim.plant.faults.read_failure = True
    sim.run(0.2)
    sim.plant.faults.read_failure = False
    sim.run(0.4)
    assert sim.alarm("ACQUISITION_LOST") is None
    sim.plant.faults.read_failure = True
    sim.run(0.6)
    sim.plant.faults.read_failure = False
    sim.run(0.4)
    assert sim.alarm("ACQUISITION_LOST") and sim.state == "TRIPPED"
    o = sim.plant.outputs
    assert o.light_red and not o.light_green and not o.heating_circulation
    assert o.control_valve_percent == 0.0 and not o.cabinet_fan_disabled


@scenario(23, "Reboot with latched Level 3: IDLE, still latched, red steady")
def s23():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(5)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 30.0
    sim.run(1)
    assert sim.state == "TRIPPED"
    sim.plant.faults.temperature_offsets.clear()
    sim2 = sim.restart(params=FAST)
    sim2.run(1)
    assert sim2.state == "TRIPPED" and sim2.alarm("OVERTEMPERATURE")
    assert sim2.out("light_red")
    sim2.ctl.commands.reset(OPERATOR)
    sim2.run(0.4)
    assert sim2.state not in ("TRIPPED",)


@scenario(28, "Plant trip while writer fails: not reported as written")
def s28():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(5)
    sim.plant.faults.write_failure = True
    sim.plant.faults.read_failure = True
    sim.run(1)
    assert sim.status.write_ok is False and sim.status.written is None
    assert sim.events("OUTPUT_WRITE_FAILED")


@scenario(33, "PT4 invalid: yellow, treatment continues")
def s33():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.plant.faults.invalid_temperatures.add("PT4_B5")
    sim.run(5)
    assert sim.state == "FALLBACK_TREATMENT" and sim.alarm("PT4_INVALID")


@scenario(35, "Output write failure: TRIPPED, red steady")
def s35():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.plant.faults.write_failure = True
    sim.run(0.6)
    assert sim.state == "TRIPPED" and sim.alarm("OUTPUT_WRITE_FAILED")


@scenario(37, "Selector invalid 0.4 s recovers; 0.8 s plant Level 3")
def s37():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(30)
    sim.plant.manhole_water = True
    sim.plant.faults.selector_both = True
    sim.run(0.4)
    assert not sim.out("heating_circulation")
    sim.plant.faults.selector_both = False
    sim.run(1)
    assert sim.alarm("SELECTOR_INVALID_PERSISTENT") is None
    sim.plant.faults.selector_both = True
    sim.run(0.8)
    assert sim.alarm("SELECTOR_INVALID_PERSISTENT") and sim.state == "TRIPPED"


@scenario(38, "Controller hangs: watchdog shutdown, restart latched")
def s38():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(15)           # past the arbiter start-up pause
    assert sim.plant.outputs.heating_circulation
    sim.stall(0.8)
    assert not sim.watchdog.tripped
    sim.stall(0.4)
    assert sim.watchdog.tripped and not sim.plant.outputs.heating_circulation
    sim2 = sim.restart(params=FAST)
    sim2.run(1)
    assert sim2.alarm("WATCHDOG_RESTART") and sim2.state == "TRIPPED"
    assert sim2.events("PREVIOUS_RUN_UNCLEAN")


@scenario(46, "Writes fail for 1.2 s: no heartbeat, watchdog shutdown at 1.0 s")
def s46():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(15)
    assert sim.plant.outputs.heating_circulation
    sim.plant.faults.write_failure = True
    sim.run(0.8)
    assert not sim.watchdog.tripped
    sim.run(0.4)
    assert sim.watchdog.tripped and not sim.plant.outputs.heating_circulation
    sim.plant.faults.write_failure = False
    sim2 = sim.restart(params=FAST)
    sim2.run(1)
    assert sim2.alarm("WATCHDOG_RESTART") and sim2.state == "TRIPPED"


@scenario(39, "Corrupt fault file: plant safe frame until reinitialised and reset")
def s39():
    sim = Sim(params=FAST, bath=70.0, store=MemoryStore(corrupt=True))
    sim.run(1)
    assert sim.alarm("FAULT_STORAGE") and sim.out("light_red")
    sim.ctl.commands.reset(OPERATOR)
    sim.run(0.4)
    assert sim.alarm("FAULT_STORAGE")
    assert sim.ctl.reinitialize_fault_storage(COMMISSIONING).accepted
    sim.run(0.4)
    sim.ctl.commands.reset(OPERATOR)
    sim.run(0.4)
    assert sim.alarm("FAULT_STORAGE") is None


@scenario(40, "Logger failure: yellow, treatment continues, gap recorded")
def s40():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.history.fail = True
    sim.run(7)           # detected on the next write (sample every 5 s)
    assert sim.state == "FALLBACK_TREATMENT" and sim.alarm("LOGGER_FAILURE")
    assert sim.alarm("STATE_SAVE_FAILED") is None      # historian is not state storage
    sim.history.fail = False
    sim.run(7)           # next retry after 5 s
    gap = sim.events("LOG_GAP")
    assert gap and gap[-1]["data"]["cause"] == "STORAGE_UNAVAILABLE"
    assert gap[-1]["data"]["dropped_events"] == 0 and sim.alarm("LOGGER_FAILURE") is None


@scenario(49, "Write failure: plant Level 3, safe frame requested, not reported written")
def s49():
    sim = Sim(params=FAST, bath=84.0)
    sim.plant.tank[1] = 900.0
    to_fallback_treatment(sim)
    sim.plant.faults.write_failure = True
    sim.run(0.6)
    status = sim.status
    alarm = sim.alarm("OUTPUT_WRITE_FAILED")
    assert alarm and alarm.scope == "PLANT" and status.state == "TRIPPED"
    assert status.write_ok is False and status.written is None
    assert len(sim.events("OUTPUT_WRITE_FAILED")) == 1, "first failure logged once"
    sim.plant.faults.write_failure = False
    sim.run(0.4)
    o = sim.plant.outputs
    assert not (o.transfer_pump_1 or o.overflow_valve_1_open or o.pasteurizer_isolation_open)
    assert o.light_red and sim.events("OUTPUT_WRITE_RECOVERED")
    sim.ctl.commands.reset(OPERATOR)
    sim.run(0.4)
    assert sim.alarm("OUTPUT_WRITE_FAILED") is None


@scenario(50, "Y4 feedback lost: release inhibited at once, Level 3 after 60 s")
def s50():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.plant.faults.y4_feedback_invalid = True
    sim.run(0.4)
    assert sim.state == "FALLBACK_QUALIFY", sim.state
    assert not sim.out("pasteurizer_isolation_open") and sim.out("control_valve_percent") == 0
    sim.run(55)
    assert sim.state != "FALLBACK_TREATMENT" and sim.alarm("Y4_FEEDBACK_LOST") is None
    assert sim.run(10, until=state_is("TRIPPED"))
    assert sim.alarm("Y4_FEEDBACK_LOST")


@scenario(51, "Save failure: plant Level 3, starts and resets refused, retried")
def s51():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(5)
    sim.store.fail_saves = True
    sim.ctl.commands.stop(OPERATOR)          # a change that must be saved
    sim.run(0.4)
    assert sim.alarm("STATE_SAVE_FAILED") and sim.state == "TRIPPED"
    assert sim.alarm("LOGGER_FAILURE") is None          # state storage is not the historian
    assert sim.status.state_saved is False and sim.status.stop_hold
    assert not sim.ctl.commands.reset(OPERATOR).accepted
    assert not sim.ctl.commands.start(OPERATOR).accepted
    sim.store.fail_saves = False
    sim.run(0.4)
    assert sim.status.state_saved and sim.store.load().stop_hold
    assert sim.ctl.commands.reset(OPERATOR).accepted
    sim.run(0.4)
    assert sim.alarm("STATE_SAVE_FAILED") is None


@scenario(53, "Ordinary power loss: IDLE with persisted faults and holds, no watchdog fault")
def s53():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(5)
    sim.plant.tank[1] = 900.0
    sim.plant.faults.no_pulses.add("flow_sensor_1")
    assert sim.run(40, until=lambda s: s.alarm("TRANSFER_1_NO_FLOW") is not None)
    sim.ctl.commands.stop(OPERATOR)
    sim.run(1)
    sim2 = sim.restart(params=FAST)
    sim2.run(1)
    assert sim2.state == "IDLE" and sim2.status.stop_hold
    assert sim2.alarm("TRANSFER_1_NO_FLOW") and sim2.alarm("WATCHDOG_RESTART") is None
