"""Shared alarm types for the plant controller."""

from dataclasses import dataclass
from enum import Enum


class AlarmSeverity(str, Enum):
    WARNING = "WARNING"   # yellow: attention required, operation may continue
    TRIP = "TRIP"         # red: affected scope is stopped


class AlarmScope(str, Enum):
    TRANSFER_1 = "TRANSFER_1"
    TRANSFER_2 = "TRANSFER_2"
    MANHOLE_DRAIN = "MANHOLE_DRAIN"
    OVERFLOW = "OVERFLOW"
    PASTEURIZER = "PASTEURIZER"
    MODEL = "MODEL"
    PLANT = "PLANT"


class AlarmEventKind(str, Enum):
    RESTORED = "RESTORED"
    RAISED = "RAISED"
    CLEARED = "CLEARED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESET = "RESET"


@dataclass(frozen=True)
class AlarmDefinition:
    """Fixed metadata, registered once per alarm code."""

    code: str
    scope: AlarmScope
    severity: AlarmSeverity
    message: str
    # Latching alarms stay listed after the condition clears until reset.
    latching: bool = True
    # Shown to the operator: what to check before resetting.
    recovery: str = ""
    # Fault level (spec section 12): 1 self-clearing, 2 latched equipment,
    # 3 latched critical. Defaults follow severity and latching.
    level: int | None = None

    @property
    def fault_level(self) -> int:
        if self.level is not None:
            return self.level
        if self.severity == AlarmSeverity.TRIP:
            return 3
        return 2 if self.latching else 1


@dataclass(frozen=True)
class AlarmState:
    """Immutable view of one listed alarm."""

    definition: AlarmDefinition
    # The condition is present now.
    active: bool
    # Seen by an operator; acknowledging does NOT reset the interlock.
    acknowledged: bool
    raised_at: float
    cleared_at: float | None
    occurrences: int

    @property
    def code(self) -> str:
        return self.definition.code

    @property
    def scope(self) -> AlarmScope:
        return self.definition.scope

    @property
    def severity(self) -> AlarmSeverity:
        return self.definition.severity

    @property
    def awaiting_reset(self) -> bool:
        return not self.active


@dataclass(frozen=True)
class AlarmEvent:
    number: int
    time: float
    cycle: int
    kind: AlarmEventKind
    code: str
    scope: AlarmScope
    severity: AlarmSeverity
    message: str
