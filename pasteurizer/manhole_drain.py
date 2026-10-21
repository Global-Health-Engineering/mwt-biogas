"""Manhole drainage decisions; no hardware writes.

Commissioning pending:
- Determine whether run-on after the float drops is appropriate.
- Current run-on duration is zero.
"""

import json
import time
from pathlib import Path


class ManholeDrainController:
    def __init__(self, roles_path=None):
        if roles_path is None:
            roles_path = (
                Path(__file__).resolve().parents[1]
                / "config/equipment_roles.json"
            )

        with open(roles_path) as file:
            roles = json.load(file)

        self.level_input = roles["levels"]["manhole_water_present"]
        self.output = roles["pumps"]["manhole_drain"]
        self.reset()

    def reset(self):
        """Reset at startup; later read failures should use process()."""
        self.mode = None
        self.manual_inhibited = False
        self._previous_reached = None

    def request_manual(self, enabled):
        """START clears inhibition; STOP inhibits automatic drainage."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if self.mode != "MANUAL":
            raise RuntimeError("Manual commands require MANUAL mode")

        self.manual_inhibited = not enabled

    def process(self, signals):
        mode_state = signals.get("mode", {})
        mode = (
            mode_state.get("value")
            if mode_state.get("valid") is True
            else "INVALID"
        )
        self.mode = mode

        # AUTO immediately cancels any manual stop override.
        if mode == "AUTO":
            self.manual_inhibited = False

        sampled_at = signals.get("sampled_at")
        fresh = (
            isinstance(sampled_at, (int, float))
            and not isinstance(sampled_at, bool)
            and 0 <= time.monotonic() - sampled_at <= 0.5
        )

        reading = signals.get("levels", {}).get(self.level_input, {})
        reached = reading.get("reached")
        available = (
            fresh
            and not signals.get("sampling_interrupted", False)
            and reading.get("valid") is True
            and isinstance(reached, bool)
        )

        if not available:
            # Unknown input must not manufacture a False -> True edge.
            self._previous_reached = None
            on = False
            reason = "Level reading or sampling unavailable"
        else:
            new_water_event = (
                self._previous_reached is False and reached is True
            )
            self._previous_reached = reached

            if mode == "MANUAL" and new_water_event:
                self.manual_inhibited = False

            if mode not in ("AUTO", "MANUAL"):
                on = False
                reason = "Mode OFF or invalid"
            elif self.manual_inhibited:
                on = False
                reason = "Automatic drainage manually inhibited"
            elif not reached:
                on = False
                reason = "No water at level switch"
            else:
                on = True
                reason = "Water detected; drainage requested"

        return {
            "manhole_drain": {
                "output": self.output,
                "on": on,
                "reason": reason,
                "manual_inhibited": self.manual_inhibited,
                "level_reached": reached if available else None,
            }
        }