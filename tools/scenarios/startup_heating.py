"""Start-up, preheat and heating scenarios."""

from scenarios.harness import FAST, Sim, make_config, scenario, state_is


@scenario(1, "Cold start: preheat, break-before-make, HEAT_UP at 64 C")
def s1():
    sim = Sim(params=FAST, bath=40.0)
    sim.run(1)
    assert sim.state == "PREHEAT_3KW", sim.state
    sim.run(9.0)
    assert not sim.out("heater_3kw"), "3 kW must wait 10 s after start"
    sim.run(1.4)
    assert sim.out("heater_3kw")
    assert not sim.out("heating_circulation") and not sim.out("transfer_pump_1")
    assert sim.run(600, until=state_is("HEAT_UP"))
    assert sim.plant.bath_c >= 63.9
    sim.run(0.4)
    assert not sim.out("heating_circulation"), "circulation waits 10 s after 3 kW off"
    sim.run(10.4)
    assert sim.out("heating_circulation")


@scenario(2, "Warm start skips preheat")
def s2():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(1)
    assert sim.state in ("HEAT_UP", "FALLBACK_QUALIFY"), sim.state


@scenario(3, "Model disabled: FALLBACK_QUALIFY, green and red blinking")
def s3():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(2)   # flow sensor 3 needs two counter samples (1 s apart)
    assert sim.state == "FALLBACK_QUALIFY", sim.state
    greens, reds = set(), set()
    for _ in range(10):
        sim.run(0.2)
        greens.add(sim.out("light_green"))
        reds.add(sim.out("light_red"))
    assert greens == {True, False} and reds == {True, False}
    assert sim.status.dwell_remaining_s is None or sim.plant.bath_c >= 80


@scenario(4, "Dwell resets when PT5 drops below 80 C")
def s4():
    sim = Sim(params=FAST, bath=82.0)
    sim.run(30)
    remaining = sim.status.dwell_remaining_s
    assert remaining is not None and remaining < 35, remaining
    sim.plant.faults.temperature_offsets["PT5_B6"] = -10.0
    sim.run(1)
    assert sim.status.dwell_remaining_s is None
    sim.plant.faults.temperature_offsets.clear()
    sim.run(1)
    assert sim.status.dwell_remaining_s > 58


@scenario(10, "1 kW: fallback thresholds and minimum ON/OFF times")
def s10():
    from pasteurizer.heating import AssistHeater
    cfg = make_config().control.heater_1kw
    h = AssistHeater(cfg)
    assert h.update(81.0, 82, 84, True, 0) is True
    assert h.update(85.0, 82, 84, True, 30) is True, "minimum ON time"
    assert h.update(85.0, 82, 84, True, 61) is False
    assert h.update(80.0, 82, 84, True, 90) is False, "minimum OFF time"
    assert h.update(80.0, 82, 84, True, 122) is True
    assert h.update(80.0, 82, 84, False, 123) is False, "fault overrides minimum ON"


@scenario(14, "Preheat over its limit trips (limit shortened for test)")
def s14():
    sim = Sim(params=dict(bath_heat_capacity_j_k=1e9), bath=40.0,
              control={"preheat.max_duration_s": 60.0})
    assert sim.run(80, until=state_is("TRIPPED"))
    assert sim.alarm("PREHEAT_TIMEOUT")
