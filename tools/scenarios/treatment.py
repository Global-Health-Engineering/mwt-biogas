"""Treatment entry, control, stop criteria and model/fallback scenarios."""

from pasteurizer.model_interface import ModelStatus
from scenarios.harness import (
    FAST,
    MODEL_ON,
    OPERATOR,
    Sim,
    scenario,
    state_is,
    to_fallback_treatment,
)


@scenario(5, "Release: green blinks during Y3 opening, steady once Y4 open")
def s5():
    sim = Sim(params=FAST, bath=84.0)
    assert sim.run(200, until=state_is("FALLBACK_TREATMENT"))
    sim.run(0.4)
    assert sim.out("pasteurizer_isolation_open") and sim.out("control_valve_percent") == 0.0
    greens = set()
    for _ in range(10):
        sim.run(0.2)
        greens.add(sim.out("light_green"))
    assert greens == {True, False}, "green must blink while Y4 is commanded closed"
    sim.run(9)
    assert sim.out("control_valve_percent") == 10.0
    sim.run(0.6)
    greens, reds = set(), set()
    for _ in range(10):
        sim.run(0.2)
        greens.add(sim.out("light_green"))
        reds.add(sim.out("light_red"))
    assert greens == {True} and reds == {True, False}


@scenario(6, "Step controller: 1 % every 5 s outside 79.5-80.5 C")
def s6():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    positions = []
    for _ in range(6):
        sim.run(5)
        positions.append(sim.out("control_valve_percent"))
    steps = {round(b - a, 3) for a, b in zip(positions, positions[1:])}
    assert steps <= {-1.0, 0.0, 1.0}, positions


@scenario(7, "PT3 below 65 C: 1.8 s no stop, 2.4 s stop")
def s7():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.run(20)
    sim.plant.faults.temperature_offsets["PT3_B4"] = -30.0
    sim.run(1.8)
    sim.plant.faults.temperature_offsets.clear()
    sim.run(0.4)
    assert sim.state == "FALLBACK_TREATMENT", sim.state
    sim.plant.faults.temperature_offsets["PT3_B4"] = -30.0
    sim.run(2.6)
    assert sim.state in ("HEAT_UP", "FALLBACK_QUALIFY"), sim.state
    assert not sim.out("pasteurizer_isolation_open")
    assert sim.out("heating_circulation")


@scenario(8, "Low treatment flow: one retry, then TRIPPED; reset restores retry")
def s8():
    sim = Sim(params=dict(FAST, y4_full_flow_l_min=2.0), bath=84.0)
    to_fallback_treatment(sim)
    assert sim.run(30, until=state_is("FALLBACK_QUALIFY", "HEAT_UP"))
    sim.run(0.4)
    assert sim.alarm("LOW_TREATMENT_FLOW") and sim.status.retry_available is False
    sim.run(5)
    assert sim.out("heating_circulation"), "heating continues during the retry wait"
    assert sim.run(200, until=state_is("TRIPPED"))
    assert sim.alarm("LOW_TREATMENT_FLOW_RETRY_FAILED")
    assert not sim.out("heating_circulation")
    sim.run(0.4)         # cause confirmed absent in a later cycle
    sim.ctl.commands.reset(OPERATOR)
    sim.run(1)
    assert sim.status.retry_available and sim.state != "TRIPPED"


@scenario(9, "Supply loss: back within 120 s resumes; 130 s stops")
def s9():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    saved = sim.plant.effluent_l
    sim.plant.effluent_l = 0.0
    sim.run(1)
    assert sim.state == "FALLBACK_QUALIFY" and not sim.out("pasteurizer_isolation_open")
    assert sim.out("heating_circulation")
    sim.run(89)
    sim.plant.effluent_l = saved
    assert sim.run(80, until=state_is("FALLBACK_TREATMENT"))
    sim.plant.effluent_l = 0.0
    assert sim.run(140, until=state_is("STOPPING", "IDLE"))
    sim.run(40)
    assert sim.state == "IDLE" and not sim.out("heating_circulation")


@scenario(24, "Model stale / UNRELIABLE: fallback; automatic return after 60 s")
def s24():
    sim = Sim(params=FAST, bath=84.0, control=MODEL_ON)
    sim.model(achievable_flow_l_min=0.8)
    assert sim.run(300, until=state_is("MODEL_TREATMENT")), sim.status.state_reason
    sim.run(20)
    sim.model_template["status"] = ModelStatus.UNRELIABLE
    sim.run(0.4)
    assert sim.state == "FALLBACK_QUALIFY" and sim.alarm("MODEL_UNRELIABLE")
    assert not sim.out("pasteurizer_isolation_open")
    assert sim.run(200, until=state_is("FALLBACK_TREATMENT"))
    sim.run(15)
    sim.model_template["status"] = ModelStatus.TRACKING
    before = sim.out("control_valve_percent")
    assert sim.run(70, until=state_is("MODEL_TREATMENT"))
    after = sim.out("control_valve_percent")
    assert abs(after - before) <= 5.0, (before, after)
    assert sim.alarm("MODEL_UNRELIABLE") is None


@scenario(25, "Stale model result (> 3 s) leaves model treatment")
def s25():
    sim = Sim(params=FAST, bath=84.0, control=MODEL_ON)
    sim.model(achievable_flow_l_min=0.8)
    assert sim.run(300, until=state_is("MODEL_TREATMENT"))
    sim.model_template = None                        # results stop arriving
    sim.run(3.6)
    assert sim.state in ("FALLBACK_QUALIFY", "HEAT_UP"), sim.state


@scenario(29, "Model achievable flow below 0.3 L/min withholds treatment")
def s29():
    sim = Sim(params=FAST, bath=84.0, control=MODEL_ON)
    sim.model(achievable_flow_l_min=0.8)
    assert sim.run(300, until=state_is("MODEL_TREATMENT"))
    sim.model_template["achievable_flow_l_min"] = 0.25
    sim.run(0.4)
    assert sim.state in ("HEAT_UP", "READY") and not sim.out("pasteurizer_isolation_open")


@scenario(31, "Three interruptions in 30 min: yellow, no stop")
def s31():
    sim = Sim(params=FAST, bath=84.0)
    for _ in range(3):
        to_fallback_treatment(sim)
        sim.plant.faults.temperature_offsets["PT3_B4"] = -30.0
        sim.run(3)
        sim.plant.faults.temperature_offsets.clear()
    assert sim.alarm("REPEATED_INTERRUPTIONS") and sim.state != "TRIPPED"


@scenario(32, "Flow above fallback maximum: close, yellow, requalify")
def s32():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.plant.p.y4_full_flow_l_min = 20.0
    assert sim.run(5, until=state_is("FALLBACK_QUALIFY"))
    sim.run(0.4)
    assert sim.alarm("FALLBACK_FLOW_HIGH") and not sim.out("pasteurizer_isolation_open")


@scenario(34, "PT2 invalid in MODEL_TREATMENT: fallback qualification")
def s34():
    sim = Sim(params=FAST, bath=84.0, control=MODEL_ON)
    sim.model(achievable_flow_l_min=0.8)
    assert sim.run(300, until=state_is("MODEL_TREATMENT"))
    sim.plant.faults.invalid_temperatures.add("PT2_B3")
    sim.run(0.4)
    assert sim.state == "FALLBACK_QUALIFY" and sim.alarm("MODEL_INPUT_INVALID")
    assert sim.run(100, until=state_is("FALLBACK_TREATMENT"))


@scenario(42, "No treatment path: HEAT_UP, treatment isolated")
def s42():
    sim = Sim(params=FAST, bath=84.0)
    sim.plant.faults.counter_unstable.add("flow_sensor_3")
    sim.run(5)
    assert sim.state == "HEAT_UP" and not sim.out("pasteurizer_isolation_open")


@scenario(43, "After a supply-loss shutdown AUTO waits in IDLE for supply")
def s43():
    sim = Sim(params=FAST, bath=84.0)
    sim.run(5)
    sim.plant.effluent_l = 0.0
    assert sim.run(140, until=state_is("STOPPING", "IDLE"))
    sim.run(60)
    assert sim.state == "IDLE" and not sim.out("heating_circulation")
    sim.plant.effluent_l = 500.0
    sim.run(1)
    assert sim.state in ("HEAT_UP", "FALLBACK_QUALIFY")


@scenario(44, "Supply loss in the same scan as low flow: no low-flow warning")
def s44():
    sim = Sim(params=dict(FAST, y4_full_flow_l_min=2.0), bath=84.0)
    to_fallback_treatment(sim)
    sim.plant.effluent_l = 0.0
    sim.run(2)
    assert sim.alarm("LOW_TREATMENT_FLOW") is None and sim.status.retry_available


@scenario(45, "Interruption count excludes supply loss and GUI STOP")
def s45():
    sim = Sim(params=FAST, bath=84.0)
    for _ in range(2):
        to_fallback_treatment(sim)
        sim.plant.faults.temperature_offsets["PT3_B4"] = -30.0
        sim.run(3)
        sim.plant.faults.temperature_offsets.clear()
    to_fallback_treatment(sim)
    sim.plant.effluent_l = 0.0
    sim.run(2)
    sim.plant.effluent_l = 500.0
    to_fallback_treatment(sim)
    sim.ctl.commands.stop(OPERATOR)
    sim.run(2)
    assert sim.ctl.treatment.interruption_count(sim.clock.monotonic()) == 2 and sim.alarm("REPEATED_INTERRUPTIONS") is None
