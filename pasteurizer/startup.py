"""Select startup heating stages without writing to hardware."""

import json
import math
import time
from pathlib import Path


class StartupController:
    def __init__(self, config_directory=None):
        if config_directory is None:
            config_directory = (
                Path(__file__).resolve().parents[1] / "config"
            )

        def load(name):
            with open(Path(config_directory) / name) as file:
                return json.load(file)

        settings = load("startup_settings.json")
        roles = load("equipment_roles.json")

        self.sensor = settings["water_temperature_sensor"]
        self.cold_threshold = settings["cold_start_below_celsius"]
        self.complete_threshold = settings["preheat_complete_at_celsius"]

        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in (self.cold_threshold, self.complete_threshold)
        ):
            raise ValueError("Startup thresholds must be finite numbers")

        if self.cold_threshold >= self.complete_threshold:
            raise ValueError("Cold threshold must be below completion threshold")

        self.heater_output = roles["heaters"]["startup_water_preheater"]["output"]
        self.reset()

    def reset(self):
        """Explicitly reset the startup sequence, not on every sample."""
        self.stage = "IDLE"
        self.fault = None

    def process(self, signals, *, heating_permitted, power_ready):
        """power_ready means conflicting loads have already been switched off.

        The central controller must establish this permission before enabling
        the 3 kW heater. This module cannot verify physical relay operation.
        """
        mode = signals.get("mode", {})
        automatic = (
            mode.get("valid") is True and mode.get("value") == "AUTO"
        )

        # Faults remain latched across mode changes.
        if not automatic:
            if self.fault is None:
                self.stage = "IDLE"
            return self._result(False, False, "Automatic startup inactive")

        if self.fault is not None:
            return self._result(False, False, self.fault)

        sampled_at = signals.get("sampled_at")
        fresh = (
            isinstance(sampled_at, (int, float))
            and not isinstance(sampled_at, bool)
            and 0 <= time.monotonic() - sampled_at <= 0.5
        )

        reading = signals.get("temperatures", {}).get(self.sensor, {})
        temperature = reading.get("celsius")
        valid_temperature = (
            reading.get("valid") is True
            and isinstance(temperature, (int, float))
            and not isinstance(temperature, bool)
            and math.isfinite(temperature)
        )

        if (
            not fresh
            or signals.get("sampling_interrupted", False)
            or not valid_temperature
        ):
            self.stage = "FAULT"
            self.fault = "Startup temperature or sampling unavailable"
            return self._result(False, False, self.fault)

        if self.stage == "IDLE":
            self.stage = (
                "PREHEAT_3KW"
                if temperature < self.cold_threshold
                else "SECOND_STAGE"
            )

        if (
            self.stage == "PREHEAT_3KW"
            and temperature >= self.complete_threshold
        ):
            self.stage = "SECOND_STAGE"

        # Stage 2 never automatically falls back to 3 kW heating.
        # Its operating temperatures and PT3 faults belong to later modules.
        if heating_permitted is not True:
            return self._result(False, False, "Heating permission withheld")

        if self.stage == "PREHEAT_3KW":
            if power_ready is not True:
                return self._result(
                    False, False, "Waiting for conflicting loads to switch off"
                )
            return self._result(True, False, "Cold-water 3 kW preheating")

        return self._result(
            False, True, "Second-stage heating may take control"
        )

    def _result(self, heater_on, second_stage, reason):
        preheating = self.stage == "PREHEAT_3KW"
        return {
            "stage": self.stage,
            "fault": self.fault,
            "reason": reason,
            "heater_3kw": {
                "output": self.heater_output,
                "on": heater_on,
            },
            "block_transfer_pumps": preheating,
            "block_heater_1kw": preheating,
            "block_heating_circulation": preheating,
            "second_stage_permitted": second_stage,
            "pasteurization_permitted": False,
        }