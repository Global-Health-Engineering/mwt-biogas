"""3 kW heater draws no power: thermostat reached (preheat ends) or heater fault.

The solar probe checks that the load rises by 70 % of the heater's power when
the 3 kW is switched on (NO_LOAD otherwise) and that it does not fall by that
much while it runs (LOAD_LOST after 20 s). With PT5 >= 40 °C that is the
heater's own thermostat: PREHEAT ends in HEAT_UP. Below 40 °C it is a fault:
warning, and after 3 times in a row the 3 kW is blocked until reset. The
simulated 3 kW has a thermostat (heater_3kw_thermostat_c) and a dead fault.
All plant numbers are PRETEND values.
"""

from pasteurizer.config import ConfigError
from pasteurizer.modes import Mode
from scenarios.harness import COMMISSIONING, FAST, OPERATOR, Sim, make_config, scenario, state_is

FAST_RETRY = {"energy.probe_retry_delay_s": 60.0}


def probe_events(sim, result):
    return [e for e in sim.events("ENERGY_PROBE")
            if e["data"]["heater"] == "heater_3kw" and e["data"]["event"] == result]


@scenario(155, "Thermostat opens while the 3 kW runs (PT5 >= 40 °C): load lost 20 s -> preheat ends, HEAT_UP")
def s155():
    sim = Sim(params={**FAST, "heater_3kw_thermostat_c": 46.0}, bath=30.0)
    assert sim.run(60, until=state_is("PREHEAT_3KW"))
    assert sim.run(120, until=lambda s: s.plant.heater_draws("heater_3kw"))
    assert sim.run(900, until=lambda s: s.plant.heater_3kw_thermostat_open)
    t_open = sim.clock.monotonic()
    assert sim.out("heater_3kw"), "still commanded on: the thermostat cut it locally"
    assert sim.run(60, until=state_is("HEAT_UP")), sim.status.state_reason
    assert 19.0 <= sim.clock.monotonic() - t_open <= 32.0, sim.clock.monotonic() - t_open
    assert "thermostat reached" in sim.status.state_reason
    lost = probe_events(sim, "LOAD_LOST")
    assert lost and lost[-1]["data"]["load_drop_w"] >= 2100
    event = sim.events("HEATER_3KW_THERMOSTAT")[-1]["data"]
    assert 45.0 <= event["pt5"] <= 48.0, event
    assert not sim.alarm("HEATER_3KW_NO_LOAD"), "the thermostat is not a fault"
    sim.run(2)
    assert not sim.out("heater_3kw") and sim.out("heating_circulation"), \
        "HEAT_UP: 3 kW off, circulation (biogas) on"


@scenario(156, "Probe with the thermostat already open (PT5 >= 40 °C): NO_LOAD, not a solar fail -> HEAT_UP")
def s156():
    sim = Sim(params={**FAST, "heater_3kw_thermostat_c": 46.0}, bath=50.0)
    sim.run(0.2)
    assert sim.plant.heater_3kw_thermostat_open
    assert sim.run(120, until=state_is("HEAT_UP")), sim.status.state_reason
    no_load = probe_events(sim, "NO_LOAD")
    assert no_load and "No electrical load registered" in no_load[-1]["data"]["reason"]
    assert not probe_events(sim, "FAIL"), "never judged on solar"
    assert "thermostat reached" in sim.status.state_reason
    assert not sim.alarm("HEATER_3KW_NO_LOAD")


@scenario(157, "Dead 3 kW in cold water (PT5 < 40 °C): warning each time; 3rd time blocked, AUTO continues in HEAT_UP; reset")
def s157():
    sim = Sim(params=FAST, bath=25.0, arbiter=FAST_RETRY)
    sim.plant.faults.heater_3kw_dead = True
    assert sim.run(120, until=lambda s: probe_events(s, "NO_LOAD"))
    assert sim.alarm("HEATER_3KW_NO_LOAD")
    assert sim.state in ("PREHEAT_3KW", "WAITING_FOR_SOLAR"), sim.state
    assert not probe_events(sim, "FAIL")
    assert sim.run(400, until=lambda s: len(probe_events(s, "NO_LOAD")) >= 3)
    sim.run(0.4)
    assert sim.alarm("HEATER_3KW_NO_LOAD_REPEATED")
    to_heat_up = [e["data"] for e in sim.events("STATE_TRANSITION")
                  if e["data"]["new"] == "HEAT_UP"]
    assert to_heat_up and "blocked" in to_heat_up[-1]["reason"], to_heat_up
    assert sim.state in ("HEAT_UP", "FALLBACK_QUALIFY"), sim.state
    counts = [e["data"]["count"] for e in sim.events("HEATER_3KW_NO_LOAD")]
    assert counts == [1, 2, 3], counts
    # Blocked: AUTO no longer tries the 3 kW.
    sim.run(200)
    assert len(probe_events(sim, "NO_LOAD")) == 3 and not sim.out("heater_3kw")
    # Repaired and reset: the block and the warning clear.
    sim.plant.faults.heater_3kw_dead = False
    assert sim.ctl.commands.reset(OPERATOR).accepted
    sim.run(0.4)
    assert not sim.alarm("HEATER_3KW_NO_LOAD_REPEATED") and not sim.alarm("HEATER_3KW_NO_LOAD")
    assert sim.ctl.heater_load.count == 0


@scenario(158, "No-load warning clears itself when a later probe sees the load; the count starts again")
def s158():
    sim = Sim(params=FAST, bath=25.0, arbiter=FAST_RETRY)
    sim.plant.faults.heater_3kw_dead = True
    assert sim.run(120, until=lambda s: probe_events(s, "NO_LOAD"))
    assert sim.alarm("HEATER_3KW_NO_LOAD")
    sim.plant.faults.heater_3kw_dead = False
    assert sim.run(200, until=lambda s: probe_events(s, "PASS"))
    sim.run(0.4)
    assert not sim.alarm("HEATER_3KW_NO_LOAD") and sim.ctl.heater_load.count == 0
    assert sim.state == "PREHEAT_3KW" and sim.out("heater_3kw")
    stored = [r for r in sim.history.events if r["kind"] == "HEATER_3KW_NO_LOAD"]
    assert stored, "the warning stays in the log"


@scenario(159, "MANUAL 3 kW ON with the thermostat open: NO_LOAD logged as thermostat (no warning), 3 kW off")
def s159():
    sim = Sim(params={**FAST, "heater_3kw_thermostat_c": 46.0}, bath=50.0)
    sim.plant.mode = Mode.MANUAL
    sim.run(11)
    assert sim.ctl.commands.manual_request(COMMISSIONING, "heater_3kw", True).accepted
    assert sim.run(80, until=lambda s: probe_events(s, "NO_LOAD"))
    sim.run(0.4)
    assert sim.events("HEATER_3KW_THERMOSTAT")
    assert not sim.alarm("HEATER_3KW_NO_LOAD")
    assert not sim.out("heater_3kw"), "off for the retry delay"
    assert sim.state == "IDLE"


@scenario(160, "Load-check settings: 1 kW is not load-checked; unknown heaters and bad fractions are rejected")
def s160():
    config = make_config()
    assert config.arbiter.energy.probe_load_check_heaters == ("heater_3kw",)
    assert config.control.preheat.heater_3kw_thermostat_min_pt5_c == 40.0
    for bad in ({"energy.probe_load_check_heaters": ["heater_2kw"]},
                {"energy.probe_load_check_heaters": "heater_3kw"},
                {"energy.probe_min_load_rise_fraction": 0.0}):
        try:
            make_config(arbiter=bad)
            raise AssertionError(f"accepted {bad}")
        except ConfigError:
            pass
