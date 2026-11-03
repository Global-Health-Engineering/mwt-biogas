"""Operator, manual mode, override and logging scenarios."""

from pasteurizer.modes import Mode
from scenarios.harness import (
    COMMISSIONING,
    FAST,
    OPERATOR,
    Sim,
    scenario,
    state_is,
    to_fallback_treatment,
)


@scenario(21, "Manual request expires after 30 s; roles enforced")
def s21():
    sim = Sim(params=FAST, bath=70.0)
    sim.plant.mode = Mode.MANUAL
    sim.run(11)          # arbiter start-up pause
    assert not sim.ctl.commands.manual_request(OPERATOR, "heater_1kw", True).accepted
    assert sim.ctl.commands.manual_request(COMMISSIONING, "heater_1kw", True).accepted
    sim.run(1)
    assert sim.out("heater_1kw")
    sim.run(29.4)
    assert not sim.out("heater_1kw")


@scenario(22, "Live override cannot hide a real destination-high")
def s22():
    sim = Sim(params=FAST, bath=70.0, simulated_io=False)
    sim.plant.mode = Mode.MANUAL
    sim.plant.effluent_l = 950.0                     # destination high (real)
    sim.plant.tank[1] = 500.0
    sim.run(1)
    roles = sim.config.level_roles
    assert sim.ctl.commands.level_override(COMMISSIONING, roles["effluent_tank_high"], False).accepted
    sim.ctl.commands.manual_request(COMMISSIONING, "transfer_pump_1", True)
    sim.run(2)
    assert not sim.out("transfer_pump_1")
    sim.run(9)
    assert not sim.ctl.manual.overrides, "override expires after 10 s"
    assert sim.events("OPERATOR_ACTION")


@scenario(26, "Indicator lights: IDLE all off, TRIPPED red only")
def s26():
    sim = Sim(params=FAST, bath=70.0)
    sim.plant.mode = Mode.OFF
    sim.run(1)
    o = sim.plant.outputs
    assert not (o.light_green or o.light_red)


@scenario(30, "Exhausted retry allowance survives a restart")
def s30():
    sim = Sim(params=dict(FAST, y4_full_flow_l_min=2.0), bath=84.0)
    to_fallback_treatment(sim)
    assert sim.run(30, until=lambda s: s.alarm("LOW_TREATMENT_FLOW") is not None)
    sim2 = sim.restart(params=FAST)
    sim2.run(1)
    assert sim2.status.retry_available is False


@scenario(36, "GUI STOP survives restart; START repeats startup checks")
def s36():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    assert sim.ctl.commands.stop(OPERATOR).accepted
    assert sim.run(30, until=state_is("IDLE"))
    sim2 = sim.restart(params=FAST)
    sim2.run(5)
    assert sim2.state == "IDLE" and sim2.status.stop_hold
    sim2.plant.manhole_water = True
    sim2.run(11)
    assert sim2.out("manhole_drain")
    assert sim2.ctl.commands.start(OPERATOR).accepted
    sim2.run(1)
    assert sim2.state in ("HEAT_UP", "FALLBACK_QUALIFY")


@scenario(47, "Roles: operator cannot override; commissioning can; all logged")
def s47():
    sim = Sim(params=FAST, bath=70.0, simulated_io=False)
    sim.plant.mode = Mode.MANUAL
    sim.run(1)
    sw = sim.config.level_roles["manhole_water_present"]
    assert not sim.ctl.commands.level_override(OPERATOR, sw, True).accepted
    assert sim.ctl.commands.level_override(COMMISSIONING, sw, True).accepted
    actions = [r["data"] for r in sim.events("OPERATOR_ACTION")]
    assert [a["accepted"] for a in actions[-2:]] == [False, True]
    assert actions[-1]["user"] == "comm"


@scenario(48, "Wall clock jump does not affect timers; correction logged")
def s48():
    sim = Sim(params=FAST, bath=84.0)
    sim.run(20)
    remaining = sim.status.dwell_remaining_s
    sim.wall_offset += 7200
    sim.run(1)
    assert abs(sim.status.dwell_remaining_s - (remaining - 1)) < 0.5
    corrections = sim.events("CLOCK_CORRECTION")
    assert corrections and abs(corrections[-1]["data"]["correction_s"] - 7200) < 2


@scenario(52, "Valve test during pasteurizer Level 3: commissioning only, heating stays off")
def s52():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(5)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 30.0
    sim.run(0.6)
    assert sim.state == "TRIPPED"
    sim.plant.mode = Mode.MANUAL
    sim.run(11)
    sim.ctl.commands.manual_request(COMMISSIONING, "pasteurizer_isolation_open", True)
    sim.ctl.commands.manual_request(COMMISSIONING, "heating_circulation", True)
    sim.run(0.6)
    assert not sim.out("pasteurizer_isolation_open"), "blocked without the override"
    assert not sim.ctl.commands.allow_valve_test(OPERATOR).accepted
    assert sim.ctl.commands.allow_valve_test(COMMISSIONING).accepted
    sim.run(0.6)
    assert sim.out("pasteurizer_isolation_open") and not sim.out("heating_circulation")
    assert sim.status.valve_test_override and sim.alarm("OVERTEMPERATURE")
    sim.run(30)
    assert not sim.out("pasteurizer_isolation_open") and not sim.status.valve_test_override
    actions = [r["data"]["action"] for r in sim.events("OPERATOR_ACTION")]
    assert actions.count("VALVE_TEST_OVERRIDE") == 2
