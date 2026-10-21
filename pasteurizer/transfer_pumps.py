"""Decide transfer-pump operation without writing to hardware."""

import json
import time
from pathlib import Path


class TransferPumpController:
    def __init__(self, roles_path=None):
        if roles_path is None:
            roles_path = (
                Path(__file__).resolve().parents[1]
                / "config/equipment_roles.json"
            )

        with open(roles_path) as file:
            roles = json.load(file)

        self.level_roles = roles["levels"]
        self.outputs = {
            line: roles["pumps"][f"line_{line}_transfer"]
            for line in (1, 2)
        }
        self.reset()

    def reset(self):
        self.mode = None
        self.running = {1: False, 2: False}
        self.manual_requests = {1: False, 2: False}

    def request_manual(self, line, enabled):
        """Called later by the webapp; requests still pass safety checks."""
        if line not in self.running:
            raise ValueError("Line must be 1 or 2")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if self.mode != "MANUAL":
            raise RuntimeError("Manual commands require MANUAL mode")

        self.manual_requests[line] = enabled

    def process(self, signals, *, heater_3kw_active):
        """heater_3kw_active is the last commanded heater state."""
        mode_state = signals.get("mode", {})
        mode = (
            mode_state.get("value")
            if mode_state.get("valid") is True
            else "INVALID"
        )

        sampled_at = signals.get("sampled_at")
        fresh = (
            isinstance(sampled_at, (int, float))
            and 0 <= time.monotonic() - sampled_at <= 0.5
        )

        transition = mode != self.mode
        if transition or not fresh or signals.get("sampling_interrupted"):
            self.reset()

        self.mode = mode
        levels = signals.get("levels", {})

        def level(role):
            name = self.level_roles[role]
            reading = levels.get(name, {})
            reached = reading.get("reached")
            if reading.get("valid") is not True:
                return None
            return reached if isinstance(reached, bool) else None

        destination_high = level("effluent_tank_high")
        decisions = {}

        for line in (1, 2):
            low = level(f"line_{line}_tank_low")
            high = level(f"line_{line}_tank_high")
            reason = None

            if not fresh or signals.get("sampling_interrupted"):
                reason = "Sampling unavailable or interrupted"
            elif mode not in ("AUTO", "MANUAL"):
                reason = "Mode OFF or invalid"
            elif transition:
                reason = "Mode changed; requests cleared"
            elif not isinstance(heater_3kw_active, bool):
                reason = "3 kW heater state unavailable"
            elif heater_3kw_active:
                reason = "3 kW heater active"
            elif destination_high is None or low is None:
                reason = "Required level reading unavailable"
            elif destination_high:
                reason = "Effluent tank high"
            elif high is True and low is False:
                reason = "Source level switches inconsistent"
            elif not low:
                reason = "Source tank below low switch"
            elif mode == "AUTO" and high is None:
                reason = "Source high-level reading unavailable"

            if reason is not None:
                self.running[line] = False
                self.manual_requests[line] = False
            elif mode == "MANUAL":
                self.running[line] = self.manual_requests[line]
                reason = (
                    "Manual start requested"
                    if self.running[line] else "No manual start request"
                )
            else:
                if high:
                    self.running[line] = True
                reason = (
                    "Automatic transfer"
                    if self.running[line] else "Waiting for high level"
                )

            decisions[f"line_{line}_transfer"] = {
                "output": self.outputs[line],
                "on": self.running[line],
                "reason": reason,
            }

        return decisions