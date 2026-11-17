"""Output arbitration: turn requested commands into one safe, complete frame.

Rules, applied in this order every cycle (settings in arbiter_settings.json):

1. Fixed overrides: the cabinet fan cannot be disabled unless
   cabinet_fan_disable_allowed is true (False = local thermostat in control).
2. Permissions: a request without permission is removed. Permissions only
   allow requests; they never create one.
   Feed pump run-on (normal stop): when the effluent feed pump GPST-PU-02 is
   no longer requested but still permitted, was written on, and Y3/Y4 close
   in this frame, the pump stays on until Y3/Y4 have been written closed for
   feed_pump.run_on_after_valves_closed_seconds. Without permission (trips,
   dry run, closed-valve cutoff) it goes off in the same frame as the valves.
3. Preheat exclusion: while the 3 kW heater is requested, every output in
   preheat_exclusion.excluded_outputs is removed (transfer pumps, 1 kW
   heater, manhole drain, effluent feed pump). The heating-water circulation is not
   excluded: it pulses together with the 3 kW in PREHEAT_3KW. Overflow
   valves Y1/Y2 are not affected.
4. Energy (solar): the electric heaters (1 kW, 3 kW, both) run only with
   the solar permission from pasteurizer/energy.py (surplus, SOC, timers,
   inverter data), when energy.solar_permission_enabled is true. The
   permission is checked for exactly the heaters still requested after rule
   3; both together are never permitted (no combined rule yet), so when both
   are requested the larger permitted one stays. Losing the 1 kW permission
   switches off only the 1 kW. The biogas heating (heating_circulation) is
   not affected.
5. Switching pause (break-before-make): the 3 kW heater may only switch on
   when every excluded output has been off in the *written* frames for
   switching_pause_seconds; excluded outputs may only switch on when the
   heater has been off for the same time. At start-up every output is
   assumed possibly on, so the pause also applies after a restart.
6. Isolation before control valve: Y4 opens (> 0 %) only if Y3 is requested
   open and has been written open for open_before_control_valve_seconds.
   When Y3 is not requested open, Y4 is closed in the same frame.
   Feed pump sequence (AUTO, feed_pump_sequence=True): the pump starts only
   when Y3 is requested open and has been written open for
   feed_pump.start_after_isolation_open_seconds; Y4 opens only while the pump
   is requested and has been written on for
   feed_pump.control_valve_after_pump_seconds. In MANUAL the order is not
   forced (Y3/Y4 and the pump can be tested separately).
7. Indicators are generated here, never passed through (spec section 11):
   green steady only with treatment release (permission held and Y4
   commanded open), blinking in heat-up, qualification and during the Y3
   opening delay; green double blink (two short pulses, then a pause) while
   WAITING_FOR_SOLAR; red steady for a Level 3, blinking on the fallback
   path; yellow for any Level 1/2 alarm.

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
ENERGY_HEATERS = ("heater_1kw", "heater_3kw")


def clock_text(seconds) -> str:
    seconds = max(0, int(math.ceil(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds >= 60 else f"{seconds} s"
FEED_PUMP_COMMAND = "effluent_feed_pump"
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


def _valves_closed_frame(out: dict) -> bool:
    return not out[ISOLATION_COMMAND] and not _active(out[CONTROL_VALVE_COMMAND])


def _valves_closed(commands: OutputCommands) -> bool:
    return (not commands.pasteurizer_isolation_open
            and not _active(commands.control_valve_percent))


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
        self._feed_pump_on_since = None
        self._valves_closed_since = None
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
            if getattr(commands, FEED_PUMP_COMMAND):
                if self._feed_pump_on_since is None:
                    self._feed_pump_on_since = now
            else:
                self._feed_pump_on_since = None
            if _valves_closed(commands):
                if self._valves_closed_since is None:
                    self._valves_closed_since = now
            else:
                self._valves_closed_since = None
            self._last_written = commands
        else:
            self._isolation_open_since = None
            self._feed_pump_on_since = None
            self._valves_closed_since = None
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
        energy=None,
        plant_safe: bool = False,
        feed_pump_sequence: bool = False,
    ) -> ArbitrationResult:
        """feed_pump_sequence: enforce Y3 -> feed pump -> Y4 (AUTO)."""
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
        if not settings.cabinet_fan_disable_allowed:
            block(FAN_COMMAND, "FAN_DISABLE_NOT_ALLOWED",
                  "Cabinet fan stays under thermostat control")

        # 2. Permissions -------------------------------------------------
        for name in PERMISSIONED_COMMANDS:
            if not getattr(permissions, name):
                block(name, "PERMISSION_DENIED",
                      permissions.reason(name) or "No permission")
        run_on = self._feed_pump_run_on(out, requested, permissions, notes, now)

        # 3. Preheat exclusion ------------------------------------------
        heater = settings.preheat_heater
        excluded = settings.preheat_excluded_outputs

        if out[heater]:
            for name in excluded:
                block(name, "PREHEAT_EXCLUSION",
                      f"Not allowed while {heater} is requested")

        # 4. Energy (solar permission) -----------------------------------
        if settings.energy.solar_permission_enabled:
            self._energy_rule(out, energy, block)

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

        if feed_pump_sequence:
            self._feed_pump_sequence(out, block, now, run_on=run_on)

        # 7. Indicators --------------------------------------------------
        out.update(self._indicators(indicators, now))

        commands = OutputCommands(**out)
        return ArbitrationResult(commands, requested, tuple(notes))

    def _feed_pump_run_on(self, out, requested, permissions, notes, now) -> bool:
        """Normal stop: keep the pump on briefly while Y3/Y4 close."""
        name = FEED_PUMP_COMMAND
        last = self._last_written
        if (out[name] or not getattr(permissions, name) or last is None
                or not getattr(last, name) or not _valves_closed_frame(out)):
            return False
        run_on = self.settings.feed_pump_run_on_after_valves_closed_seconds
        since = self._valves_closed_since
        closed_for = 0.0 if since is None else now - since
        if closed_for >= run_on:
            return False
        out[name] = True
        notes.append(ArbitrationNote(
            output=name, requested=getattr(requested, name), granted=True,
            rule="FEED_PUMP_RUN_ON",
            message="Feed pump runs on while Y3/Y4 close",
            remaining_s=run_on - closed_for))
        return True

    def _feed_pump_sequence(self, out, block, now, *, run_on=False):
        """AUTO: Y3 open for its travel time -> pump -> after a delay Y4."""
        s = self.settings
        pump = FEED_PUMP_COMMAND
        if out[pump] and not run_on:
            wait = s.feed_pump_start_after_isolation_open_seconds
            since = self._isolation_open_since
            if not out[ISOLATION_COMMAND]:
                block(pump, "FEED_PUMP_SEQUENCE", "Feed pump starts only with Y3 requested open")
            elif since is None:
                block(pump, "FEED_PUMP_SEQUENCE",
                      "Waiting for Y3 open command to be written", wait)
            elif now - since < wait:
                block(pump, "FEED_PUMP_SEQUENCE", "Waiting for Y3 to open (travel time)",
                      wait - (now - since))
        if _active(out[CONTROL_VALVE_COMMAND]):
            wait = s.feed_pump_control_valve_after_pump_seconds
            since = self._feed_pump_on_since
            if not out[pump]:
                block(CONTROL_VALVE_COMMAND, "FEED_PUMP_NOT_RUNNING",
                      "Y4 opens only with the feed pump running")
            elif since is None:
                block(CONTROL_VALVE_COMMAND, "FEED_PUMP_STARTING",
                      "Waiting for the feed pump command to be written", wait)
            elif now - since < wait:
                block(CONTROL_VALVE_COMMAND, "FEED_PUMP_STARTING",
                      "Waiting for the feed pump to run", wait - (now - since))

    @staticmethod
    def _energy_rule(out, energy, block):
        """energy: EnergyStatus of this cycle (None: not evaluated -> no heating)."""
        heaters = [name for name in ENERGY_HEATERS if out[name]]
        if not heaters:
            return
        if energy is None:
            for name in heaters:
                block(name, "ENERGY_UNKNOWN", "Solar permission not evaluated")
            return
        if energy.permitted(heaters):
            return
        if len(heaters) > 1:
            keep = next((name for name in ("heater_3kw", "heater_1kw")
                         if energy.permitted([name])), None)
            reason = energy.reason(heaters)
            for name in heaters:
                if name != keep:
                    block(name, "ENERGY_SOLAR", reason)
            return
        mode = energy.modes[heaters[0]]
        reason = mode.reason
        if mode.timer_s is not None:
            reason = f"{reason} {clock_text(mode.timer_s)}"
        # Remaining time when it is known to end with a permission: the retry
        # delay before the next solar probe.
        block(heaters[0], "ENERGY_SOLAR", reason,
              mode.timer_s if mode.state == "RETRY_WAIT" else None)

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
        elif state == ProcessState.WAITING_FOR_SOLAR:
            green = "double_blink"
        else:
            green = "off"
        red = "blinking" if inputs.fallback_path and state in (
            ProcessState.FALLBACK_QUALIFY,
            ProcessState.FALLBACK_TREATMENT,
            ProcessState.STOPPING,
        ) else "off"
        return {"green": green, "yellow": yellow, "red": red}

    def double_blink_on(self, now: float) -> bool:
        """Two pulses (on, off, on), then the pause, repeating. Each pulse and
        gap is longer than the scan cycle, so every one reaches the lamp."""
        pulse = self.settings.solar_wait_pulse_seconds
        phase = now % (3 * pulse + self.settings.solar_wait_pause_seconds)
        return phase < pulse or 2 * pulse <= phase < 3 * pulse

    def _indicators(self, inputs: IndicatorInputs, now: float) -> dict:
        period = self.settings.heat_up_blink_period_seconds
        blink_on = (now % period) < period / 2
        lamp = {"off": False, "steady": True, "blinking": blink_on,
                "double_blink": self.double_blink_on(now)}
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
