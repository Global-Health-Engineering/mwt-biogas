"""Output arbitration: turn requested commands into one safe, complete frame.

Rules, applied in this order every cycle (settings in arbiter_settings.json):

1. Fixed overrides: the biogas compressor stays off while compressor_enabled
   is false; the cabinet fan cannot be disabled unless
   cabinet_fan_disable_allowed is true (False = local thermostat in control).
2. Permissions: a request without permission is removed. Permissions only
   allow requests; they never create one.
3. Preheat exclusion: while the 3 kW heater is requested, every output in
   preheat_exclusion.excluded_outputs is removed (transfer pumps, 1 kW
   heater, heating circulation, manhole drain, compressor). Overflow valves
   Y1/Y2 are not affected.
4. Energy: each heater has its own battery permission
   (energy.min_battery_percent), checked once battery_level_available is
   true. Losing the 1 kW permission switches off only the 1 kW.
5. Switching pause (break-before-make): the 3 kW heater may only switch on
   when every excluded output has been off in the *written* frames for
   switching_pause_seconds; excluded outputs may only switch on when the
   heater has been off for the same time. At start-up every output is
   assumed possibly on, so the pause also applies after a restart.
6. Isolation before control valve: Y4 opens (> 0 %) only if Y3 is requested
   open and has been written open for open_before_control_valve_seconds.
   When Y3 is not requested open, Y4 is closed in the same frame.
7. Indicators are generated here, never passed through (spec section 11):
   green steady only with treatment release (permission held and Y4
   commanded open), blinking in heat-up, qualification and during the Y3
   opening delay; red steady for a Level 3, blinking on the fallback path;
   yellow for any Level 1/2 alarm.

A plant-wide Level 3 replaces all of this with the plant safe command frame
(spec section 12): everything off, Y1/Y2/Y3 closed, Y4 0 %, fan disable
False, green off, red on, yellow per remaining Level 1/2 alarms.

The arbiter does not generate permissions, run state transitions or prove
physical actuation. Call record_write() after every write attempt so that the
pause timers follow what was actually sent to the hardware.
"""

from dataclasses import asdict, dataclass, fields
import math

from pasteurizer.config import PlantConfig, load_config
from pasteurizer.output_commands import (
    CONTROL_VALVE_COMMAND,
    INDICATOR_COMMANDS,
    PERMISSIONED_COMMANDS,
    SWITCHED_COMMANDS,
    OutputCommands,
    OutputPermissions,
)
from pasteurizer.process_state import HEAT_UP_STATES, TREATMENT_STATES, ProcessState


ISOLATION_COMMAND = "pasteurizer_isolation_open"
COMPRESSOR_COMMAND = "biogas_compressor"
FAN_COMMAND = "cabinet_fan_disabled"


@dataclass(frozen=True)
class IndicatorInputs:
    process_state: ProcessState
    # Any Level 1/2 alarm present (yellow).
    warning: bool
    # Any Level 3 active or latched (red steady).
    trip: bool
    # Treatment permission held and Y4 commanded open (green steady).
    release_permitted: bool = False
    # The fallback path is active (red blinking).
    fallback_path: bool = False


@dataclass(frozen=True)
class ArbitrationNote:
    """Why a requested output differs from the granted one."""

    output: str
    requested: bool | float
    granted: bool | float
    rule: str
    message: str
    # Seconds until this rule would stop blocking, when that is known.
    remaining_s: float | None = None


@dataclass(frozen=True)
class ArbitrationResult:
    commands: OutputCommands
    requested: OutputCommands
    notes: tuple[ArbitrationNote, ...]

    def blocked(self, output: str) -> tuple[ArbitrationNote, ...]:
        return tuple(note for note in self.notes if note.output == output)

    def to_dict(self) -> dict:
        return {
            "commands": asdict(self.commands),
            "requested": asdict(self.requested),
            "notes": [asdict(note) for note in self.notes],
        }


def _active(value) -> bool:
    """A command counts as 'on' when True or, for the valve, above 0 %."""
    if isinstance(value, bool):
        return value
    return value > 0.0


def _off(value):
    return False if isinstance(value, bool) else 0.0


class OutputArbiter:
    def __init__(self, config: PlantConfig | None = None, *, start_time: float):
        if not math.isfinite(start_time):
            raise ValueError("start_time must be finite")
        self.config = config if config is not None else load_config()
        self.settings = self.config.arbiter

        # Physical state is unknown at start-up: treat everything as possibly
        # on at start_time, so the switching pause applies after a restart.
        self._last_on_at = {name: float(start_time) for name in SWITCHED_COMMANDS}
        self._isolation_open_since = None
        self._last_written: OutputCommands | None = None
        self._last_time = float(start_time)

    # ------------------------------------------------------------------
    # Write feedback
    # ------------------------------------------------------------------

    @property
    def last_written(self) -> OutputCommands | None:
        """Last frame the writer reported as accepted (not physical feedback)."""
        return self._last_written

    def record_write(self, commands: OutputCommands, now: float, success: bool):
        """Report the outcome of writing a frame to the hardware.

        On failure the physical state is uncertain: every output is treated
        as possibly on now (so the switching pause restarts), and Y3 is
        treated as not (yet) open.
        """
        if not isinstance(commands, OutputCommands):
            raise TypeError("Expected OutputCommands")
        if type(success) is not bool:
            raise ValueError("success must be True or False")
        self._check_time(now)

        previous = self._last_written
        for name in SWITCHED_COMMANDS:
            was_on = previous is None or getattr(previous, name)
            if not success or getattr(commands, name) or was_on:
                # Covers on, switch-off moments and uncertain states.
                self._last_on_at[name] = now

        if success:
            if commands.pasteurizer_isolation_open:
                if self._isolation_open_since is None:
                    self._isolation_open_since = now
            else:
                self._isolation_open_since = None
            self._last_written = commands
        else:
            self._isolation_open_since = None
            self._last_written = None

    def possibly_on(self, output: str, now: float) -> bool:
        """True if output was on (or uncertain) within the switching pause."""
        return now - self._last_on_at[output] < self.settings.switching_pause_seconds

    # ------------------------------------------------------------------
    # Arbitration
    # ------------------------------------------------------------------

    def _check_time(self, now):
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if now < self._last_time:
            raise ValueError("Time must not move backwards")
        self._last_time = float(now)

    def arbitrate(
        self,
        requested: OutputCommands,
        permissions: OutputPermissions,
        indicators: IndicatorInputs,
        now: float,
        *,
        battery_percent: float | None = None,
        plant_safe: bool = False,
    ) -> ArbitrationResult:
        if not isinstance(requested, OutputCommands):
            raise TypeError("Expected OutputCommands")
        if not isinstance(permissions, OutputPermissions):
            raise TypeError("Expected OutputPermissions")
        if not isinstance(indicators, IndicatorInputs):
            raise TypeError("Expected IndicatorInputs")
        self._check_time(now)

        settings = self.settings

        if plant_safe:
            return self._plant_safe_frame(requested, indicators)

        out = asdict(requested)
        notes: list[ArbitrationNote] = []

        def block(name, rule, message, remaining=None):
            if not _active(out[name]):
                return
            notes.append(ArbitrationNote(
                output=name,
                requested=getattr(requested, name),
                granted=_off(out[name]),
                rule=rule,
                message=message,
                remaining_s=remaining,
            ))
            out[name] = _off(out[name])

        # 1. Fixed overrides --------------------------------------------
        if not settings.compressor_enabled:
            block(COMPRESSOR_COMMAND, "COMPRESSOR_DISABLED",
                  "Compressor operating strategy not defined")
        if not settings.cabinet_fan_disable_allowed:
            block(FAN_COMMAND, "FAN_DISABLE_NOT_ALLOWED",
                  "Cabinet fan stays under thermostat control")

        # 2. Permissions -------------------------------------------------
        for name in PERMISSIONED_COMMANDS:
            if not getattr(permissions, name):
                block(name, "PERMISSION_DENIED",
                      permissions.reason(name) or "No permission")

        # 3. Preheat exclusion ------------------------------------------
        heater = settings.preheat_heater
        excluded = settings.preheat_excluded_outputs

        if out[heater]:
            for name in excluded:
                block(name, "PREHEAT_EXCLUSION",
                      f"Not allowed while {heater} is requested")

        # 4. Energy ------------------------------------------------------
        if settings.battery_level_available:
            for name, minimum in settings.min_battery_percent.items():
                if (
                    not isinstance(battery_percent, (int, float))
                    or isinstance(battery_percent, bool)
                    or not math.isfinite(battery_percent)
                ):
                    block(name, "ENERGY_UNKNOWN", "Battery level unavailable")
                elif battery_percent < minimum:
                    block(name, "ENERGY_LOW",
                          f"Battery {battery_percent:.0f} % below "
                          f"{minimum:.0f} % for {name}")

        # 5. Switching pause --------------------------------------------
        pause = settings.switching_pause_seconds

        def remaining(name):
            return max(0.0, pause - (now - self._last_on_at[name]))

        if out[heater]:
            waiting = [name for name in excluded if self.possibly_on(name, now)]
            if waiting:
                block(heater, "SWITCHING_PAUSE",
                      "Waiting for conflicting loads to be off: "
                      + ", ".join(waiting),
                      max(remaining(name) for name in waiting))
        elif self.possibly_on(heater, now):
            for name in excluded:
                block(name, "SWITCHING_PAUSE",
                      f"Waiting after {heater} switched off", remaining(heater))

        # 6. Y3 before Y4 ------------------------------------------------
        if _active(out[CONTROL_VALVE_COMMAND]):
            settle = settings.isolation_open_before_control_valve_seconds
            if not out[ISOLATION_COMMAND]:
                block(CONTROL_VALVE_COMMAND, "ISOLATION_NOT_OPEN",
                      "Y4 opens only with Y3 requested open")
            elif self._isolation_open_since is None:
                block(CONTROL_VALVE_COMMAND, "ISOLATION_OPENING",
                      "Waiting for Y3 open command to be written", settle)
            elif now - self._isolation_open_since < settle:
                block(CONTROL_VALVE_COMMAND, "ISOLATION_OPENING",
                      "Waiting for Y3 to open",
                      settle - (now - self._isolation_open_since))

        # 7. Indicators --------------------------------------------------
        out.update(self._indicators(indicators, now))

        commands = OutputCommands(**out)
        return ArbitrationResult(commands, requested, tuple(notes))

    @staticmethod
    def safe_frame() -> OutputCommands:
        """Plant safe command frame (all process outputs off/closed, red steady)."""
        return OutputCommands(light_red=True)

    def _plant_safe_frame(self, requested, indicators) -> ArbitrationResult:
        safe = OutputCommands(
            light_red=True,
            light_yellow=bool(indicators.warning),
        )
        notes = tuple(
            ArbitrationNote(name, getattr(requested, name), getattr(safe, name),
                            "PLANT_SAFE_FRAME", "Plant-wide Level 3 fault")
            for name in PERMISSIONED_COMMANDS
            if _active(getattr(requested, name))
        )
        return ArbitrationResult(safe, requested, notes)

    @staticmethod
    def indicator_modes(inputs: IndicatorInputs, *, plant_safe: bool = False) -> dict:
        """Lamp modes: "off", "steady" or "blinking" (published for the GUI)."""
        state = inputs.process_state
        yellow = "steady" if inputs.warning else "off"
        if plant_safe or inputs.trip:
            return {"green": "off", "yellow": yellow, "red": "steady"}
        if inputs.release_permitted and state in TREATMENT_STATES:
            green = "steady"
        elif state in HEAT_UP_STATES or state in TREATMENT_STATES:
            # Treatment states without release = Y3 opening delay.
            green = "blinking"
        else:
            green = "off"
        red = "blinking" if inputs.fallback_path and state in (
            ProcessState.FALLBACK_QUALIFY,
            ProcessState.FALLBACK_TREATMENT,
            ProcessState.STOPPING,
        ) else "off"
        return {"green": green, "yellow": yellow, "red": red}

    def _indicators(self, inputs: IndicatorInputs, now: float) -> dict:
        period = self.settings.heat_up_blink_period_seconds
        blink_on = (now % period) < period / 2
        lamp = {"off": False, "steady": True, "blinking": blink_on}
        modes = self.indicator_modes(inputs)
        return {
            "light_green": lamp[modes["green"]],
            "light_yellow": lamp[modes["yellow"]],
            "light_red": lamp[modes["red"]],
        }


# Sanity check at import: indicators are exactly the non-permissioned fields.
assert {f.name for f in fields(OutputCommands)} == (
    set(PERMISSIONED_COMMANDS) | set(INDICATOR_COMMANDS)
)
