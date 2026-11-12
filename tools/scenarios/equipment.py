"""Transfer pump, drain, overflow and battery scenarios."""

from pasteurizer.modes import Mode
from scenarios.harness import (
    FAST,
    Sim,
    scenario,
    state_is,
    to_fallback_treatment,
)


@scenario(17, "AUTO -> OFF during treatment: STOPPING then IDLE; drain active")
def s17():
    sim = Sim(params=FAST, bath=84.0)
    to_fallback_treatment(sim)
    sim.plant.mode = Mode.OFF
    sim.run(0.4)
    assert sim.state == "STOPPING" and not sim.out("heating_circulation")
    assert sim.run(30, until=state_is("IDLE"))
    sim.plant.manhole_water = True
    sim.run(11)
    assert sim.out("manhole_drain"), "drain works in OFF"


@scenario(18, "Overflow valve: opens after 5 s detection; closes after 10 s min open and 2 s clear")
def s18():
    sim = Sim(params=FAST, bath=70.0)
    roles = sim.config.level_roles
    sw = roles["line_1_tank_overflow"]
    opened = lambda s: s.out("overflow_valve_1_open")
    closed = lambda s: not s.out("overflow_valve_1_open")
    # A 3 s splash does not open the valve.
    sim.plant.faults.forced_switches[sw] = True
    assert not sim.run(3.0, until=opened), "opened before the 5 s confirmation"
    sim.plant.faults.forced_switches[sw] = False
    assert not sim.run(10.0, until=opened), "short detection must not open later"
    # 5 s of detection opens; clear 2 s after opening -> min_open_s (10 s) decides.
    sim.plant.faults.forced_switches[sw] = True
    t_detect = sim.clock.monotonic()
    assert sim.run(8.0, until=opened)
    assert 4.9 <= sim.clock.monotonic() - t_detect <= 5.8, sim.clock.monotonic() - t_detect
    t_open = sim.clock.monotonic()
    sim.run(2.0)
    sim.plant.faults.forced_switches[sw] = False
    assert sim.run(15.0, until=closed)
    assert 9.8 <= sim.clock.monotonic() - t_open <= 10.8, sim.clock.monotonic() - t_open
    # Covered for 20 s after opening -> closes 2 s after the switch clears.
    sim.plant.faults.forced_switches[sw] = True
    assert sim.run(8.0, until=opened)
    sim.run(20.0)
    assert sim.out("overflow_valve_1_open")
    sim.plant.faults.forced_switches[sw] = False
    t_clear = sim.clock.monotonic()
    assert sim.run(6.0, until=closed)
    assert 1.9 <= sim.clock.monotonic() - t_clear <= 2.8, sim.clock.monotonic() - t_clear


@scenario(19, "Pump 1 ON without pulses: Level 2 after 10 s + 10 s; others continue")
def s19():
    sim = Sim(params=FAST, bath=84.0)
    sim.plant.tank[1] = 900.0
    sim.plant.tank[2] = 900.0
    sim.plant.faults.no_pulses.add("flow_sensor_1")
    sim.run(12)
    assert sim.out("transfer_pump_1") and sim.out("transfer_pump_2")
    assert sim.run(25, until=lambda s: s.alarm("TRANSFER_1_NO_FLOW") is not None)
    sim.run(0.4)
    assert not sim.out("transfer_pump_1") and sim.out("transfer_pump_2")
    assert sim.state != "TRIPPED"
    sim.run(30)
    assert sim.alarm("TRANSFER_1_NO_FLOW"), "no automatic retry"


@scenario(20, "Low positive transfer flow: yellow, pump runs, clears")
def s20():
    sim = Sim(params=FAST, bath=70.0)
    sim.plant.tank[2] = 900.0
    sim.plant.transfer_flow_scale[2] = 0.1          # 0.5 L/min
    assert sim.run(45, until=lambda s: s.alarm("TRANSFER_2_LOW_FLOW") is not None)
    assert sim.out("transfer_pump_2") and sim.out("light_yellow")
    sim.plant.transfer_flow_scale[2] = 1.0
    sim.run(3)
    assert sim.alarm("TRANSFER_2_LOW_FLOW") is None


@scenario(27, "Transfer measurement unavailable: 8 s recovers, 12 s Level 2")
def s27():
    sim = Sim(params=FAST, bath=70.0)
    sim.plant.tank[1] = 900.0
    sim.run(12)
    assert sim.out("transfer_pump_1")
    sim.plant.faults.counter_unstable.add("flow_sensor_1")
    sim.run(3)
    assert not sim.out("transfer_pump_1")
    sim.run(5)
    sim.plant.faults.counter_unstable.clear()
    sim.run(3)
    assert sim.out("transfer_pump_1"), "automatic recovery within 10 s"
    sim.plant.faults.counter_unstable.add("flow_sensor_1")
    sim.run(13)
    assert sim.alarm("TRANSFER_1_FLOW_INVALID_PERSISTENT")
    sim.plant.faults.counter_unstable.clear()
    sim.run(3)
    assert not sim.out("transfer_pump_1"), "Level 2 needs a GUI reset"


@scenario(41, "1 kW battery permission lost: only the 1 kW switches off")
def s41():
    sim = Sim(params=FAST, bath=78.0, battery=50.0,
              arbiter={"energy.battery_level_available": True,
                       "energy.min_battery_percent.heater_3kw": 60.0,
                       "energy.min_battery_percent.heater_1kw": 40.0})
    sim.run(15)
    assert sim.out("heater_1kw") and sim.out("heating_circulation")
    sim.battery = 30.0
    sim.run(0.4)
    assert not sim.out("heater_1kw") and sim.out("heating_circulation")
