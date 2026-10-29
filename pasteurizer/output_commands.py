"""Logical equipment commands, independent of hardware wiring."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class OutputCommands:
    heater_3kw: bool = False
    heater_1kw: bool = False
    heating_circulation: bool = False

    transfer_pump_1: bool = False
    transfer_pump_2: bool = False
    manhole_drain: bool = False

    overflow_valve_1_open: bool = False
    overflow_valve_2_open: bool = False
    pasteurizer_isolation_open: bool = False
    control_valve_percent: float = 0.0

    biogas_compressor: bool = False

    # False allows the cabinet fan's local thermostat to operate.
    cabinet_fan_disabled: bool = False

    light_green: bool = False
    light_yellow: bool = False
    light_red: bool = False

    def __post_init__(self):
        for name, value in vars(self).items():
            if name == "control_valve_percent":
                continue

            if type(value) is not bool:
                raise ValueError(f"{name} must be True or False")

        position = self.control_valve_percent

        if (
            type(position) not in (int, float)
            or not math.isfinite(position)
            or not 0.0 <= position <= 100.0
        ):
            raise ValueError(
                "control_valve_percent must be between 0 and 100"
            )