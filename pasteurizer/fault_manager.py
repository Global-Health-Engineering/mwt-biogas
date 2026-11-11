"""Fault catalogue and level handling on top of AlarmManager (spec section 12).

Level 1: affected equipment inhibited at once, clears by itself.
Level 2: latched; only affected equipment inhibited; reset in the GUI.
Level 3: latched critical; treatment/heating (or the plant) stopped; reset.

An escalating fault uses two codes: the Level 1 code is present while the
condition holds, and the escalated code is raised once it has held for the
configured time. The escalated alarm stays latched after the cause clears and
can be reset only once the cause was confirmed absent in the current cycle.
"""

from dataclasses import dataclass
from typing import Iterable

from pasteurizer.alarm_manager import AlarmManager
from pasteurizer.alarms import (
    AlarmDefinition,
    AlarmEvent,
    AlarmScope as S,
    AlarmSeverity,
    AlarmState,
)


def _fault(code, scope, level, message, recovery=""):
    return AlarmDefinition(
        code=code,
        scope=scope,
        severity=AlarmSeverity.TRIP if level == 3 else AlarmSeverity.WARNING,
        message=message,
        latching=level >= 2,
        recovery=recovery,
        level=level,
    )


def _transfer_faults(line):
    scope = S.TRANSFER_1 if line == 1 else S.TRANSFER_2
    return [
        _fault(f"TRANSFER_{line}_NO_FLOW", scope, 2,
               f"Pump {line} commanded ON without measured flow",
               "Check pump, flow sensor and wiring; reset with the pump off"),
        _fault(f"TRANSFER_{line}_LOW_FLOW", scope, 1,
               f"Transfer {line}: low positive flow"),
        _fault(f"TRANSFER_{line}_FLOW_INVALID", scope, 1,
               f"Transfer {line}: flow measurement unavailable; pump stopped"),
        _fault(f"TRANSFER_{line}_FLOW_INVALID_PERSISTENT", scope, 2,
               f"Transfer {line}: flow measurement unavailable for too long",
               "Restore the flow measurement, then reset"),
    ]


FAULTS = [
    # Plant-wide
    _fault("ACQUISITION_UNAVAILABLE", S.PLANT, 1, "Input acquisition failed"),
    _fault("ACQUISITION_LOST", S.PLANT, 3, "No valid input snapshot for too long",
           "Check RevPi modules and piControl, then reset"),
    _fault("SELECTOR_INVALID", S.PLANT, 1, "Mode selector invalid"),
    _fault("SELECTOR_INVALID_PERSISTENT", S.PLANT, 3, "Mode selector invalid",
           "Check selector wiring, then reset"),
    _fault("WATCHDOG_RESTART", S.PLANT, 3,
           "Controller restarted after a watchdog shutdown or abnormal stop",
           "Find the cause (event log: previous run, watchdog, exception), then reset"),
    _fault("FAULT_STORAGE", S.PLANT, 3, "Saved fault file unreadable or corrupt",
           "Restore or reinitialise fault storage, then reset"),
    _fault("LOGGER_FAILURE", S.PLANT, 1, "Event log or historian unavailable"),
    _fault("HISTORIAN_DB_RECOVERED", S.PLANT, 1,
           "Historian database was corrupt; preserved and replaced",
           "Acknowledge to clear; preserved files are kept"),
    _fault("STATE_SAVE_FAILED", S.PLANT, 3,
           "Persistent state cannot be saved; plant stopped",
           "Restore storage; saving is retried every cycle, then reset"),
    _fault("OUTPUT_WRITE_FAILED", S.PLANT, 3,
           "Output frame not confirmed as written; safe frame requested",
           "Restore output communication and valve feedback, then reset"),
    # Pasteurizer
    _fault("PT3_INVALID", S.PASTEURIZER, 1, "PT3 invalid; treatment isolated"),
    _fault("PT3_INVALID_PERSISTENT", S.PASTEURIZER, 3, "PT3 invalid",
           "Restore PT3, then reset"),
    _fault("PT5_INVALID", S.PASTEURIZER, 1, "PT5 invalid; heating off"),
    _fault("PT5_INVALID_PERSISTENT", S.PASTEURIZER, 3, "PT5 invalid",
           "Restore PT5, then reset"),
    _fault("PT6_INVALID", S.PASTEURIZER, 1, "PT6 invalid; heating off"),
    _fault("PT6_INVALID_PERSISTENT", S.PASTEURIZER, 3, "PT6 invalid",
           "Restore PT6, then reset"),
    _fault("FLOW3_INVALID", S.PASTEURIZER, 1,
           "Treatment flow measurement invalid; treatment isolated"),
    _fault("FLOW3_INVALID_PERSISTENT", S.PASTEURIZER, 3,
           "Treatment flow measurement invalid",
           "Restore flow sensor 3, then reset"),
    _fault("PT4_INVALID", S.PASTEURIZER, 1, "PT4 invalid"),
    _fault("OVERTEMPERATURE", S.PASTEURIZER, 3, "PT5 or PT6 at or above trip limit",
           "Reset when both are valid and below the reset temperature"),
    _fault("PREHEAT_TIMEOUT", S.PASTEURIZER, 3, "3 kW preheat took too long",
           "Check heater and PT5, then reset"),
    _fault("Y4_POSITION_TIMEOUT", S.PASTEURIZER, 3, "Y4 did not reach its position",
           "Check valve Y4 and its feedback, then reset"),
    _fault("Y4_FEEDBACK_INVALID", S.PASTEURIZER, 1,
           "Y4 feedback unavailable; release inhibited"),
    _fault("Y4_FEEDBACK_LOST", S.PASTEURIZER, 3, "Y4 feedback unavailable for too long",
           "Restore Y4 feedback, then reset"),
    _fault("LOW_TREATMENT_FLOW", S.PASTEURIZER, 1,
           "Treatment flow below minimum; automatic retry pending"),
    _fault("LOW_TREATMENT_FLOW_RETRY_FAILED", S.PASTEURIZER, 3,
           "Treatment flow still low after the automatic retry",
           "Check supply path and Y4, then reset"),
    _fault("FALLBACK_FLOW_HIGH", S.PASTEURIZER, 1,
           "Treatment flow above the fallback maximum"),
    _fault("REPEATED_INTERRUPTIONS", S.PASTEURIZER, 1,
           "Repeated unexpected treatment interruptions"),
    _fault("MODEL_UNRELIABLE_NO_FALLBACK", S.PASTEURIZER, 3,
           "Model UNRELIABLE and fallback unavailable",
           "Restore model or fallback prerequisites, then reset"),
    # Model
    _fault("MODEL_INPUT_INVALID", S.MODEL, 1, "PT1 or PT2 invalid; model unusable"),
    _fault("MODEL_UNRELIABLE", S.MODEL, 1, "Model UNRELIABLE; fallback in use"),
    # Overflow
    _fault("OVERFLOW_1", S.OVERFLOW, 1, "Tank 1 overflow; Y1 open"),
    _fault("OVERFLOW_2", S.OVERFLOW, 1, "Tank 2 overflow; Y2 open"),
    *_transfer_faults(1),
    *_transfer_faults(2),
]


class Persistence:
    """Measures how long a condition has held continuously."""

    def __init__(self):
        self.since = None

    def update(self, present: bool | None, now: float) -> float:
        """Seconds held; unknown (None) keeps the timer, False resets it."""
        if present is True:
            if self.since is None:
                self.since = now
            return now - self.since
        if present is False:
            self.since = None
        return 0.0 if self.since is None else now - self.since

    def reset(self):
        self.since = None


@dataclass(frozen=True)
class FaultSummary:
    warning: bool          # any Level 1/2 alarm listed (yellow)
    trip: bool             # any Level 3 listed (red steady)
    plant_trip: bool       # Level 3 in PLANT scope
    pasteurizer_trip: bool # Level 3 in PASTEURIZER scope


class FaultManager:
    def __init__(self, definitions: Iterable[AlarmDefinition] = FAULTS):
        self.alarms = AlarmManager(definitions, history_size=2000)
        self._timers: dict[str, Persistence] = {}

    # ------------------------------------------------------------------
    def start_cycle(self, now: float):
        self.now = now
        self.alarms.start_cycle(now)

    def set(self, code: str, present: bool | None):
        self.alarms.update(code, present)

    def escalating(self, code: str, escalated: str, present: bool | None,
                   delay_s: float) -> bool:
        """Update an escalating pair; returns True while the Level 1 code is present."""
        timer = self._timers.setdefault(code, Persistence())
        held = timer.update(present, self.now)
        self.set(code, present)
        if present is True and held >= delay_s:
            self.set(escalated, True)
        elif present is False:
            self.set(escalated, False)
        else:
            # Below the delay, or unknown: never raise, never clear.
            self.set(escalated, None)
        return present is True

    # ------------------------------------------------------------------
    def listed(self, code: str) -> bool:
        return any(state.code == code for state in self.alarms.current())

    def active(self, code: str) -> bool:
        return any(state.code == code and state.active
                   for state in self.alarms.current())

    def current(self, scope: S | None = None) -> tuple[AlarmState, ...]:
        return self.alarms.current(scope)

    def level_listed(self, level: int, scope: S | None = None) -> bool:
        return any(state.definition.fault_level == level
                   for state in self.alarms.current(scope))

    def summary(self) -> FaultSummary:
        states = self.alarms.current()
        return FaultSummary(
            warning=any(s.definition.fault_level in (1, 2) for s in states),
            trip=any(s.definition.fault_level == 3 for s in states),
            plant_trip=self.level_listed(3, S.PLANT),
            pasteurizer_trip=self.level_listed(3, S.PASTEURIZER),
        )

    def latched_codes(self) -> list[str]:
        return sorted(s.code for s in self.alarms.current()
                      if s.definition.fault_level >= 2)

    def restore(self, codes: Iterable[str], now: float):
        for code in codes:
            self.alarms.restore_latched(code, now)

    def reset(self, scopes=None) -> tuple[str, ...]:
        return self.alarms.reset_cleared(scopes)

    def events_after(self, number: int) -> tuple[AlarmEvent, ...]:
        return self.alarms.events_after(number)


