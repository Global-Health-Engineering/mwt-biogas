"""Track active and latched alarms without controlling hardware.

Usage per control cycle:

    manager.start_cycle(now)
    manager.update("TRANSFER_1_NO_FLOW", present=True/False/None)
    ...
    manager.reset_cleared()        # only when the operator asks

present=None means "could not be evaluated" (e.g. missing data). It never
clears an alarm, and an alarm whose condition was not freshly confirmed absent
in the current cycle cannot be reset.
"""

from collections import deque
from dataclasses import dataclass, replace
import math
from typing import Iterable

from pasteurizer.alarms import (
    AlarmDefinition,
    AlarmEvent,
    AlarmEventKind,
    AlarmScope,
    AlarmSeverity,
    AlarmState,
)


@dataclass
class _Evaluation:
    cycle: int
    present: bool | None


class AlarmManager:
    def __init__(self, definitions: Iterable[AlarmDefinition] = (),
                 history_size: int = 1000):
        if type(history_size) is not int or history_size < 1:
            raise ValueError("history_size must be a positive integer")

        self._definitions: dict[str, AlarmDefinition] = {}
        self._listed: dict[str, AlarmState] = {}
        self._evaluations: dict[str, _Evaluation] = {}
        self._history: deque[AlarmEvent] = deque(maxlen=history_size)
        self._cycle = 0
        self._now: float | None = None
        self._event_number = 0

        for definition in definitions:
            self.register(definition)

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------

    def register(self, definition: AlarmDefinition):
        """Register an alarm code once; redefining it is an error."""
        if not isinstance(definition, AlarmDefinition):
            raise TypeError("Expected an AlarmDefinition")
        existing = self._definitions.get(definition.code)
        if existing is not None and existing != definition:
            raise ValueError(
                f"Alarm {definition.code!r} is already registered differently"
            )
        self._definitions[definition.code] = definition

    def definitions(self) -> tuple[AlarmDefinition, ...]:
        return tuple(self._definitions.values())

    # ------------------------------------------------------------------
    # Cycle evaluation
    # ------------------------------------------------------------------

    def start_cycle(self, now: float):
        """Begin a control cycle. Every registered alarm should then be updated."""
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if self._now is not None and now < self._now:
            raise ValueError("Time must not move backwards")
        self._now = float(now)
        self._cycle += 1

    @property
    def cycle(self) -> int:
        return self._cycle

    def _require_cycle(self):
        if self._now is None:
            raise RuntimeError("Call start_cycle(now) first")

    def update(self, code: str, present: bool | None):
        """Report this cycle's evaluation of one alarm condition."""
        self._require_cycle()
        if present is not None and type(present) is not bool:
            raise ValueError("present must be True, False or None")
        definition = self._definitions.get(code)
        if definition is None:
            raise KeyError(f"Alarm {code!r} is not registered")

        self._evaluations[code] = _Evaluation(self._cycle, present)
        listed = self._listed.get(code)

        if present is True:
            if listed is None:
                self._listed[code] = AlarmState(
                    definition=definition, active=True, acknowledged=False,
                    raised_at=self._now, cleared_at=None, occurrences=1,
                )
                self._record(AlarmEventKind.RAISED, definition)
            elif not listed.active:
                # Returned while still latched from an earlier occurrence.
                self._listed[code] = replace(
                    listed, active=True, acknowledged=False,
                    raised_at=self._now, cleared_at=None,
                    occurrences=listed.occurrences + 1,
                )
                self._record(AlarmEventKind.RAISED, definition)

        elif present is False and listed is not None and listed.active:
            self._record(AlarmEventKind.CLEARED, definition)
            if definition.latching:
                self._listed[code] = replace(
                    listed, active=False, cleared_at=self._now,
                )
            else:
                del self._listed[code]

        # present None: unknown; keep everything as it is.

    def unevaluated(self) -> tuple[str, ...]:
        """Registered codes not updated in the current cycle (a programming gap)."""
        return tuple(
            code for code in self._definitions
            if self._evaluations.get(code) is None
            or self._evaluations[code].cycle != self._cycle
        )

    # ------------------------------------------------------------------
    # Operator actions
    # ------------------------------------------------------------------

    def acknowledge(self, code: str):
        """Mark as seen. This does not reset the alarm or its interlock."""
        listed = self._listed.get(code)
        if listed is None or listed.acknowledged:
            return
        self._listed[code] = replace(listed, acknowledged=True)
        self._record(AlarmEventKind.ACKNOWLEDGED, listed.definition)

    def _resettable(self, code: str) -> bool:
        listed = self._listed[code]
        evaluation = self._evaluations.get(code)
        return (
            not listed.active
            and evaluation is not None
            and evaluation.cycle == self._cycle
            and evaluation.present is False
        )

    def reset_cleared(self, scopes: Iterable[AlarmScope] | None = None
                      ) -> tuple[str, ...]:
        """Remove cleared alarms whose cause was confirmed absent this cycle.

        Returns the codes that were reset. Active alarms, and alarms whose
        condition is unknown or was not evaluated this cycle, remain.
        """
        self._require_cycle()
        scopes = set(scopes) if scopes is not None else None
        reset = []
        for code in list(self._listed):
            listed = self._listed[code]
            if scopes is not None and listed.scope not in scopes:
                continue
            if self._resettable(code):
                del self._listed[code]
                self._record(AlarmEventKind.RESET, listed.definition)
                reset.append(code)
        return tuple(reset)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def current(self, scope: AlarmScope | None = None) -> tuple[AlarmState, ...]:
        """Active alarms and cleared alarms awaiting reset."""
        return tuple(
            state for state in self._listed.values()
            if scope is None or state.scope == scope
        )

    def has_warning(self, scope: AlarmScope | None = None) -> bool:
        return any(state.severity == AlarmSeverity.WARNING
                   for state in self.current(scope))

    def has_trip(self, scope: AlarmScope | None = None) -> bool:
        return any(state.severity == AlarmSeverity.TRIP
                   for state in self.current(scope))

    def history(self) -> tuple[AlarmEvent, ...]:
        """Recent raised/cleared/acknowledged/reset events, oldest first."""
        return tuple(self._history)

    def events_after(self, number: int) -> tuple[AlarmEvent, ...]:
        """Events with a number greater than `number` (still in the buffer)."""
        return tuple(event for event in self._history if event.number > number)

    @property
    def last_event_number(self) -> int:
        return self._event_number

    def restore_latched(self, code: str, now: float):
        """Re-list a latched alarm saved before a restart, awaiting reset.

        Its condition is re-evaluated from fresh data in the next cycles; a
        reset still needs the cause confirmed absent.
        """
        definition = self._definitions.get(code)
        if definition is None:
            raise KeyError(f"Alarm {code!r} is not registered")
        if not definition.latching or code in self._listed:
            return
        previous = self._now
        self._now = float(now)
        self._listed[code] = AlarmState(
            definition=definition, active=False, acknowledged=False,
            raised_at=float(now), cleared_at=float(now), occurrences=1,
        )
        self._record(AlarmEventKind.RESTORED, definition)
        self._now = previous if previous is not None else float(now)

    def _record(self, kind: AlarmEventKind, definition: AlarmDefinition):
        self._event_number += 1
        self._history.append(AlarmEvent(
            number=self._event_number,
            time=self._now,
            cycle=self._cycle,
            kind=kind,
            code=definition.code,
            scope=definition.scope,
            severity=definition.severity,
            message=definition.message,
        ))
