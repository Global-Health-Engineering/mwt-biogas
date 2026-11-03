"""Desk check of the output arbiter with conflicting requests. No hardware."""

from dataclasses import replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.arbiter import IndicatorInputs, OutputArbiter
from pasteurizer.clock import FakeClock
from pasteurizer.config import load_config
from pasteurizer.output_commands import (
    PERMISSIONED_COMMANDS,
    OutputCommands,
    OutputPermissions,
)
from pasteurizer.process_state import ProcessState


config = load_config()
clock = FakeClock(0.0)
ALL = OutputPermissions.only(*PERMISSIONED_COMMANDS)


def make_arbiter(cfg=config):
    return OutputArbiter(cfg, start_time=clock.monotonic())


def run(arbiter, requested, permissions=ALL, *, seconds=0.2,
        state=ProcessState.IDLE, warning=False, trip=False,
        battery=None, write_ok=True, release=False, plant_safe=False):
    """Arbitrate and write every 0.2 s for `seconds`; return the last result."""
    result = None
    for _ in range(max(1, round(seconds / 0.2))):
        clock.advance(0.2)
        result = arbiter.arbitrate(
            requested, permissions,
            IndicatorInputs(state, warning, trip, release_permitted=release,
                            fallback_path=state in (ProcessState.FALLBACK_QUALIFY,
                                                    ProcessState.FALLBACK_TREATMENT)),
            clock.monotonic(), battery_percent=battery, plant_safe=plant_safe,
        )
        arbiter.record_write(result.commands, clock.monotonic(), write_ok)
    return result


def rules(result, output):
    return [note.rule for note in result.blocked(output)]


# 1. Preheat request together with conflicting loads ----------------------
arbiter = make_arbiter()
conflict = OutputCommands(
    heater_3kw=True, transfer_pump_1=True, heater_1kw=True,
    heating_circulation=True, manhole_drain=True, overflow_valve_1_open=True,
)
first = run(arbiter, conflict, state=ProcessState.PREHEAT_3KW)
assert first.commands.heater_3kw is False
assert rules(first, "heater_3kw") == ["SWITCHING_PAUSE"]
for name in ("transfer_pump_1", "heater_1kw", "heating_circulation", "manhole_drain"):
    assert getattr(first.commands, name) is False
    assert rules(first, name) == ["PREHEAT_EXCLUSION"], (name, first.notes)
assert first.commands.overflow_valve_1_open is True        # safety drain stays
later = run(arbiter, conflict, seconds=9.6, state=ProcessState.PREHEAT_3KW)
assert later.commands.heater_3kw is False                   # still in the pause
later = run(arbiter, conflict, seconds=0.6, state=ProcessState.PREHEAT_3KW)
assert later.commands.heater_3kw is True
assert later.commands.transfer_pump_1 is False
print("1. Conflicts removed at once; 3 kW waits 10 s with loads off; Y1 unaffected.")

# 2. After preheat: loads wait 10 s after the heater switched off ---------
after = OutputCommands(transfer_pump_1=True, heating_circulation=True)
result = run(arbiter, after, state=ProcessState.HEAT_UP)
assert result.commands.transfer_pump_1 is False
assert rules(result, "transfer_pump_1") == ["SWITCHING_PAUSE"]
result = run(arbiter, after, seconds=9.6, state=ProcessState.HEAT_UP)
assert result.commands.transfer_pump_1 is False
result = run(arbiter, after, seconds=0.6, state=ProcessState.HEAT_UP)
assert result.commands.transfer_pump_1 and result.commands.heating_circulation
print("2. Pumps and circulation start only 10 s after the 3 kW heater is off.")

# 3. Permission denied with reason ----------------------------------------
denied = OutputPermissions.only(
    "heating_circulation",
    reasons={"transfer_pump_1": "TRANSFER_1 trip: no flow"},
)
result = run(arbiter, after, denied, state=ProcessState.HEAT_UP)
assert result.commands.transfer_pump_1 is False
note = result.blocked("transfer_pump_1")[0]
assert note.rule == "PERMISSION_DENIED" and "no flow" in note.message
assert result.commands.heating_circulation is True
print(f"3. Request without permission removed: {note.message}")

# 4. Y3 before Y4 -----------------------------------------------------------
arbiter = make_arbiter()
both = OutputCommands(pasteurizer_isolation_open=True, control_valve_percent=15.0)
result = run(arbiter, both, state=ProcessState.FALLBACK_TREATMENT)
assert result.commands.pasteurizer_isolation_open is True
assert result.commands.control_valve_percent == 0.0
assert rules(result, "control_valve_percent") == ["ISOLATION_OPENING"]
result = run(arbiter, both, seconds=10.2, state=ProcessState.FALLBACK_TREATMENT)
assert result.commands.control_valve_percent == 15.0
closing = OutputCommands(control_valve_percent=15.0)
result = run(arbiter, closing, state=ProcessState.STOPPING)
assert result.commands.control_valve_percent == 0.0
assert rules(result, "control_valve_percent") == ["ISOLATION_NOT_OPEN"]
print("4. Y4 opens 10 s after Y3 is written open, and closes with Y3.")

# 5. Fixed overrides ---------------------------------------------------------
result = run(arbiter, OutputCommands(biogas_compressor=True,
                                     cabinet_fan_disabled=True))
assert not result.commands.biogas_compressor
assert not result.commands.cabinet_fan_disabled
print("5. Compressor kept off; fan stays under thermostat control.")

# 6. Indicators -----------------------------------------------------------
requested_lights = OutputCommands(light_red=True, light_green=True)
result = run(arbiter, requested_lights)
assert not (result.commands.light_red or result.commands.light_green)
greens = [run(arbiter, OutputCommands(), state=ProcessState.HEAT_UP)
          .commands.light_green for _ in range(10)]
assert True in greens and False in greens
greens = [run(arbiter, OutputCommands(), state=ProcessState.FALLBACK_TREATMENT,
              warning=True).commands.light_green for _ in range(10)]
assert True in greens and False in greens   # Y3 opening: still blinking
greens = {run(arbiter, OutputCommands(), state=ProcessState.FALLBACK_TREATMENT,
              warning=True, release=True).commands.light_green for _ in range(10)}
assert greens == {True}
reds = [run(arbiter, OutputCommands(), state=ProcessState.FALLBACK_TREATMENT,
            release=True).commands.light_red for _ in range(10)]
assert True in reds and False in reds         # fallback: red blinking
result = run(arbiter, OutputCommands(), state=ProcessState.TRIPPED,
             warning=True, trip=True)
assert (result.commands.light_green, result.commands.light_yellow,
        result.commands.light_red) == (False, True, True)
print("6. Lights: green blinks until release, steady with release; red blinks on fallback.")

# 7. Battery level hook -----------------------------------------------------
battery_config = replace(config, arbiter=replace(
    config.arbiter, battery_level_available=True,
    min_battery_percent={"heater_3kw": 50.0, "heater_1kw": 30.0},
))
arbiter = make_arbiter(battery_config)
heat = OutputCommands(heater_3kw=True)
run(arbiter, heat, seconds=10.4, state=ProcessState.PREHEAT_3KW, battery=80.0)
assert run(arbiter, heat, battery=80.0).commands.heater_3kw is True
assert rules(run(arbiter, heat, battery=None), "heater_3kw") == ["ENERGY_UNKNOWN"]
assert rules(run(arbiter, heat, battery=40.0), "heater_3kw")[0] == "ENERGY_LOW"
print("7. With battery data enabled, 3 kW needs a known level above the minimum.")

# 8. Write failure restarts the pause ---------------------------------------
arbiter = make_arbiter()
run(arbiter, OutputCommands(transfer_pump_1=True), seconds=11.0)
run(arbiter, OutputCommands(), seconds=11.0)
run(arbiter, OutputCommands(), write_ok=False)
result = run(arbiter, heat, state=ProcessState.PREHEAT_3KW)
assert rules(result, "heater_3kw") == ["SWITCHING_PAUSE"]
print("8. After a failed write, outputs count as possibly on: pause restarts.")

# 9. Plant safe command frame ------------------------------------------------
arbiter = make_arbiter()
busy = OutputCommands(heater_1kw=True, heating_circulation=True, transfer_pump_2=True,
                      overflow_valve_1_open=True, pasteurizer_isolation_open=True,
                      control_valve_percent=40.0)
result = run(arbiter, busy, state=ProcessState.TRIPPED, warning=True, trip=True,
             plant_safe=True)
c = result.commands
assert not any([c.heater_1kw, c.heating_circulation, c.transfer_pump_2,
                c.overflow_valve_1_open, c.pasteurizer_isolation_open,
                c.cabinet_fan_disabled, c.light_green])
assert c.control_valve_percent == 0.0 and c.light_red and c.light_yellow
print("9. Plant-wide trip: safe command frame, red on, yellow per warnings.")

print("All arbiter checks passed.")
