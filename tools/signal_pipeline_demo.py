"""Desk check of signals and equipment decisions with fake inputs and a fake clock.

No hardware access: raw snapshots are constructed here in the same format
as HardwareReader.read() produces.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.clock import FakeClock
from pasteurizer.config import load_config
from pasteurizer.manhole_drain import ManholeDrainController
from pasteurizer.modes import Mode
from pasteurizer.signals import SignalProcessor
from pasteurizer.startup import StartupController, StartupStage
from pasteurizer.transfer_pumps import TransferPumpController


config = load_config()
clock = FakeClock(1000.0)
signals = SignalProcessor(config, clock=clock)
transfer = TransferPumpController(config)
drain = ManholeDrainController(config)
startup = StartupController(config)
roles = config.level_roles
sequence = 0

print(f"Config hash: {config.config_hash[:12]}")

# Plant state used to build the fake snapshots.
switches = {name: False for name in config.io_mapping["digital_inputs"]}
temperatures_c = {name: 25.0 for name in config.io_mapping["temperatures"]}


def set_mode(mode):
    switches["auto_switch"] = mode == Mode.AUTO
    switches["manual_switch"] = mode == Mode.MANUAL


def set_level(role, reached):
    switches[roles[role]] = reached


def raw_snapshot(sampled_at=None):
    global sequence
    sequence += 1
    return {
        "sampled_at": clock.monotonic() if sampled_at is None else sampled_at,
        "sequence": sequence,
        "module_status": {"dio": 0, "di": 0},
        "digital_inputs": dict(switches),
        "flow_counters": {name: 0 for name in config.io_mapping["flow_counters"]},
        "temperatures": {
            name: {"raw": round(value * 10), "status": 0}
            for name, value in temperatures_c.items()
        },
        "control_valve": {
            "command": 4000, "command_status": 0,
            "feedback": config.valve.feedback_closed_raw, "feedback_status": 0,
        },
    }


def step(seconds=0.2):
    clock.advance(seconds)
    return signals.process(raw_snapshot())


def run_for(seconds, cycle=0.2):
    snapshot = None
    for _ in range(round(seconds / cycle)):
        snapshot = step(cycle)
    return snapshot


set_mode(Mode.AUTO)
run_for(1.0)

# 1. Acquisition freshness ------------------------------------------------
snap = step(0.4)                       # one missed read: tolerated
assert snap.available and not snap.acquisition.interrupted
snap = step(0.8)                       # gap beyond 0.6 s: interrupted
assert snap.acquisition.interrupted and not snap.available
snap = step()
assert snap.available
clock.advance(0.2)
stale = signals.process(raw_snapshot(sampled_at=clock.monotonic() - 1.0))
assert not stale.acquisition.valid
print("1. 0.4 s gap tolerated, 0.8 s gap interrupted, 1 s old snapshot unavailable.")
run_for(1.0)

# 2. Manhole drain delays (10 s on, 20 s off) -----------------------------
def drain_for(seconds):
    decision = None
    for _ in range(round(seconds / 0.2)):
        decision = drain.process(step(), clock.monotonic())
    return decision


set_level("manhole_water_present", True)
assert drain_for(9.8).request.on is False
assert drain_for(0.8).request.on is True   # + 0.2 s debounce
set_level("manhole_water_present", False)
assert drain_for(19.8).request.on is True
assert drain_for(0.8).request.on is False  # + 0.2 s debounce
set_level("manhole_water_present", True)
drain_for(5.0)
set_level("manhole_water_present", False)      # short splash: no start
assert drain_for(10.0).request.on is False
print("2. Drain starts after 10 s of water and stops 20 s after the level clears.")

# 3. Transfer pump hysteresis and 3 kW interlock --------------------------
set_level("effluent_tank_high", False)
set_level("line_1_tank_low", True)
set_level("line_1_tank_high", True)
snap = run_for(0.6)
transfer.process(snap, heater_3kw_active=False)
snap = step()
requests = transfer.process(snap, heater_3kw_active=False)
assert requests["transfer_pump_1"].on is True, requests
set_level("line_1_tank_high", False)       # hysteresis: keeps running
snap = run_for(0.6)
requests = transfer.process(snap, heater_3kw_active=False)
assert requests["transfer_pump_1"].on is True
requests = transfer.process(step(), heater_3kw_active=True)
assert requests["transfer_pump_1"].on is False
assert requests["transfer_pump_1"].inhibited_by == ("HEATER_3KW_ACTIVE",)
print("3. Transfer pump 1 runs on high level, holds until low, stops for 3 kW heater.")

# 4. Startup stages ---------------------------------------------------------
temperatures_c[config.startup.water_temperature_sensor] = 50.0
decision = startup.process(step(), heating_permitted=True, power_ready=True)
assert decision.stage == StartupStage.PREHEAT_3KW and decision.heater_3kw.on
temperatures_c[config.startup.water_temperature_sensor] = 64.0
decision = startup.process(step(), heating_permitted=True, power_ready=True)
assert decision.stage == StartupStage.SECOND_STAGE
assert not decision.heater_3kw.on and decision.second_stage_permitted
print("4. Startup: 3 kW preheat at 50 C, second stage released at 64 C.")

# 5. Plausibility range -----------------------------------------------------
temperatures_c["PT3_B4"] = 120.0
snap = step()
assert snap.temperatures["PT3_B4"].valid is False
print(f"5. PT3 at 120 C rejected: {snap.temperatures['PT3_B4'].error}")

# 6. Log form ---------------------------------------------------------------
import json
json.dumps(snap.to_dict(), allow_nan=False)
assert snap.to_dict()["mode"]["value"] == "AUTO"
print("6. Snapshot serialises to JSON for logging.")

print("All signal pipeline checks passed.")
