"""Typed data passed between layers.

Layer 2 (signals) produces one SignalSnapshot per cycle. Layer 3 (equipment)
returns EquipmentRequest objects keyed by OutputCommands field names. All
objects are frozen so that no consumer can change what another one sees.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum

from pasteurizer.modes import Mode
from pasteurizer.temperatures import TemperatureReading
from pasteurizer.valves import ValveFeedback


@dataclass(frozen=True)
class AcquisitionStatus:
    # False: no usable data this cycle (read failure or stale snapshot).
    valid: bool
    # True: the gap since the previous sample was too long, so debounce and
    # flow histories were restarted. Data in this snapshot is not yet trusted.
    interrupted: bool
    age_seconds: float | None
    interval_seconds: float | None
    error: str | None = None


@dataclass(frozen=True)
class ModeSignal:
    value: Mode
    valid: bool
    error: str | None = None


@dataclass(frozen=True)
class LevelSignal:
    raw: bool | None
    # True/False once debounced; None while unknown.
    reached: bool | None
    pending: bool
    valid: bool
    error: str | None = None


@dataclass(frozen=True)
class FlowSignal:
    raw_counter: int | None
    pulse_rate_hz: float | None
    flow_lpm: float | None
    measured_volume_litres: float | None
    valid: bool
    calibrated: bool
    error: str | None
    # Flow is evaluated about once per second, not every cycle.
    sampled_at: float | None


@dataclass(frozen=True)
class ValveSignals:
    feedback: ValveFeedback
    command_raw: int | None
    output_status: int | None
    output_healthy: bool


def _plain(value):
    """Convert enums inside asdict() output to their string values."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class SignalSnapshot:
    sampled_at: float
    sequence: int | None
    acquisition: AcquisitionStatus
    module_status: dict[str, int] | None
    mode: ModeSignal
    levels: dict[str, LevelSignal]
    flows: dict[str, FlowSignal]
    temperatures: dict[str, TemperatureReading]
    valve: ValveSignals

    @property
    def available(self) -> bool:
        """True when equipment decisions may rely on this snapshot."""
        return self.acquisition.valid and not self.acquisition.interrupted

    def level(self, name: str) -> bool | None:
        """Debounced level of a switch, or None when unknown."""
        reading = self.levels.get(name)
        if reading is None or reading.valid is not True:
            return None
        return reading.reached if isinstance(reading.reached, bool) else None

    def to_dict(self) -> dict:
        """JSON-compatible representation for logs."""
        return _plain(asdict(self))


@dataclass(frozen=True)
class EquipmentRequest:
    """What one equipment controller asks for, and why."""

    on: bool
    reason: str
    # Short codes of the protections that currently prevent operation,
    # e.g. ("HEATER_3KW_ACTIVE",). Empty when nothing is blocking.
    inhibited_by: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return _plain(asdict(self))
