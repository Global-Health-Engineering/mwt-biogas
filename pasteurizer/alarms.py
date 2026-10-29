"""Shared alarm definitions for the plant controller."""

from dataclasses import dataclass
from enum import Enum


class AlarmSeverity(str, Enum):
    WARNING = "WARNING"
    TRIP = "TRIP"


class AlarmScope(str, Enum):
    TRANSFER_1 = "TRANSFER_1"
    TRANSFER_2 = "TRANSFER_2"
    MANHOLE_DRAIN = "MANHOLE_DRAIN"
    PASTEURIZER = "PASTEURIZER"
    PLANT = "PLANT"


@dataclass
class Alarm:
    code: str
    scope: AlarmScope
    severity: AlarmSeverity
    message: str
    active: bool = True
    latched: bool = True