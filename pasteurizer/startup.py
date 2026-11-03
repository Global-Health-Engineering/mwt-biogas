"""Select startup heating stages without writing to hardware."""

from dataclasses import dataclass
from enum import Enum
import math

from pasteurizer.config import PlantConfig, load_config
from pasteurizer.contracts import EquipmentRequest, SignalSnapshot
from pasteurizer.modes import Mode


class StartupStage(str, Enum):
    IDLE = "IDLE"
    PREHEAT_3KW = "PREHEAT_3KW"
    SECOND_STAGE = "SECOND_STAGE"
    FAULT = "FAULT"


@dataclass(frozen=True)
class StartupDecision:
    stage: StartupStage
    fault: str | None
    reason: str
    heater_3kw: EquipmentRequest
    block_transfer_pumps: bool
    block_heater_1kw: bool
    block_heating_circulation: bool
    second_stage_permitted: bool
    pasteurization_permitted: bool


class StartupController:
    def __init__(self, config: PlantConfig | None = None):
        self.config = config if config is not None else load_config()
        settings = self.config.startup

        self.sensor = settings.water_temperature_sensor
        self.cold_threshold = settings.cold_start_below_celsius
        self.complete_threshold = settings.preheat_complete_at_celsius
        self.reset()

    def reset(self):
        """Explicitly reset the startup sequence, not on every sample."""
        self.stage = StartupStage.IDLE
        self.fault = None

    def process(self, signals: SignalSnapshot, *, heating_permitted,
                power_ready) -> StartupDecision:
        """power_ready means conflicting loads have already been switched off.

        The central controller must establish this permission before enabling
        the 3 kW heater. This module cannot verify physical relay operation.
        """
        automatic = signals.mode.valid and signals.mode.value == Mode.AUTO

        # Faults remain latched across mode changes.
        if not automatic:
            if self.fault is None:
                self.stage = StartupStage.IDLE
            return self._result(False, False, "Automatic startup inactive")

        if self.fault is not None:
            return self._result(False, False, self.fault)

        reading = signals.temperatures.get(self.sensor)
        temperature = reading.celsius if reading is not None else None
        valid_temperature = (
            reading is not None
            and reading.valid is True
            and isinstance(temperature, (int, float))
            and not isinstance(temperature, bool)
            and math.isfinite(temperature)
        )

        if not signals.available or not valid_temperature:
            self.stage = StartupStage.FAULT
            self.fault = "Startup temperature or sampling unavailable"
            return self._result(False, False, self.fault)

        if self.stage == StartupStage.IDLE:
            self.stage = (
                StartupStage.PREHEAT_3KW
                if temperature < self.cold_threshold
                else StartupStage.SECOND_STAGE
            )

        if (
            self.stage == StartupStage.PREHEAT_3KW
            and temperature >= self.complete_threshold
        ):
            self.stage = StartupStage.SECOND_STAGE

        # Stage 2 never automatically falls back to 3 kW heating.
        # Its operating temperatures and PT3 faults belong to later modules.
        if heating_permitted is not True:
            return self._result(False, False, "Heating permission withheld",
                                inhibited_by=("HEATING_NOT_PERMITTED",))

        if self.stage == StartupStage.PREHEAT_3KW:
            if power_ready is not True:
                return self._result(
                    False, False, "Waiting for conflicting loads to switch off",
                    inhibited_by=("CONFLICTING_LOADS_ACTIVE",),
                )
            return self._result(True, False, "Cold-water 3 kW preheating")

        return self._result(
            False, True, "Second-stage heating may take control"
        )

    def _result(self, heater_on, second_stage, reason, inhibited_by=()):
        preheating = self.stage == StartupStage.PREHEAT_3KW
        if self.fault is not None and not inhibited_by:
            inhibited_by = ("STARTUP_FAULT",)
        return StartupDecision(
            stage=self.stage,
            fault=self.fault,
            reason=reason,
            heater_3kw=EquipmentRequest(heater_on, reason, tuple(inhibited_by)),
            block_transfer_pumps=preheating,
            block_heater_1kw=preheating,
            block_heating_circulation=preheating,
            second_stage_permitted=second_stage,
            pasteurization_permitted=False,
        )
