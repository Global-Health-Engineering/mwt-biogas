"""Decide transfer-pump operation without writing to hardware."""

from pasteurizer.config import PlantConfig, load_config
from pasteurizer.contracts import EquipmentRequest, SignalSnapshot
from pasteurizer.modes import Mode


LINES = (1, 2)


def command_name(line: int) -> str:
    """OutputCommands field controlled for this line."""
    return f"transfer_pump_{line}"


class TransferPumpController:
    def __init__(self, config: PlantConfig | None = None):
        self.config = config if config is not None else load_config()
        self.level_roles = self.config.level_roles
        self.reset()

    def reset(self):
        self.mode = None
        self.running = {line: False for line in LINES}
        self.manual_requests = {line: False for line in LINES}

    def request_manual(self, line, enabled):
        """Called later by the webapp; requests still pass safety checks."""
        if line not in self.running:
            raise ValueError("Line must be 1 or 2")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if self.mode != Mode.MANUAL:
            raise RuntimeError("Manual commands require MANUAL mode")

        self.manual_requests[line] = enabled

    def process(self, signals: SignalSnapshot, *,
                heater_3kw_active, overridden=frozenset()) -> dict[str, EquipmentRequest]:
        """heater_3kw_active: the 3 kW heater is or may still be on.

        overridden: level switches substituted by a commissioning override;
        plausibility checks that depend on them are suppressed.
        Returns requests keyed by OutputCommands field name.
        """
        mode = signals.mode.value if signals.mode.valid else Mode.INVALID
        available = signals.available

        transition = mode != self.mode
        if transition or not available:
            self.reset()

        self.mode = mode

        def level(role):
            return signals.level(self.level_roles[role])

        destination_high = level("effluent_tank_high")
        decisions = {}

        for line in LINES:
            low = level(f"line_{line}_tank_low")
            high = level(f"line_{line}_tank_high")
            reason = None
            blocker = None

            if not available:
                reason, blocker = ("Sampling unavailable or interrupted",
                                   "SAMPLING_UNAVAILABLE")
            elif mode not in (Mode.AUTO, Mode.MANUAL):
                reason, blocker = "Mode OFF or invalid", "MODE"
            elif transition:
                reason, blocker = "Mode changed; requests cleared", "MODE_CHANGED"
            elif not isinstance(heater_3kw_active, bool):
                reason, blocker = ("3 kW heater state unavailable",
                                   "HEATER_3KW_STATE_UNKNOWN")
            elif heater_3kw_active:
                reason, blocker = "3 kW heater active", "HEATER_3KW_ACTIVE"
            elif destination_high is None or low is None:
                reason, blocker = ("Required level reading unavailable",
                                   "LEVEL_UNAVAILABLE")
            elif destination_high:
                reason, blocker = "Effluent tank high", "DESTINATION_HIGH"
            elif (high is True and low is False
                  and self.level_roles[f"line_{line}_tank_high"] not in overridden
                  and self.level_roles[f"line_{line}_tank_low"] not in overridden):
                reason, blocker = ("Source level switches inconsistent",
                                   "SOURCE_LEVELS_INCONSISTENT")
            elif not low:
                reason, blocker = "Source tank below low switch", "SOURCE_LOW"
            elif mode == Mode.AUTO and high is None:
                reason, blocker = ("Source high-level reading unavailable",
                                   "LEVEL_UNAVAILABLE")

            if reason is not None:
                self.running[line] = False
                self.manual_requests[line] = False
            elif mode == Mode.MANUAL:
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

            decisions[command_name(line)] = EquipmentRequest(
                on=self.running[line],
                reason=reason,
                inhibited_by=(blocker,) if blocker else (),
            )

        return decisions
