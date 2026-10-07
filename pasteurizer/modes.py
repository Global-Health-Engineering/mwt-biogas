"""Interpret the selector's two digital contacts."""

from enum import Enum


class Mode(str, Enum):
    OFF = "OFF"
    AUTO = "AUTO"
    MANUAL = "MANUAL"
    INVALID = "INVALID"


def process_mode(manual: bool, auto: bool) -> Mode:
    if manual and auto:
        return Mode.INVALID
    if manual:
        return Mode.MANUAL
    if auto:
        return Mode.AUTO
    return Mode.OFF