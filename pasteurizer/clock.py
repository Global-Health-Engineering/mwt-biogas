"""Monotonic time sources.

Modules never call time.monotonic() themselves. The controller passes a
time value (or a Clock) in, so that simulations and tests can control time
deterministically.
"""

import math
import time
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; never moves backwards."""


class MonotonicClock:
    """The real clock used on the RevPi."""

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Manually advanced clock for simulation and desk checks."""

    def __init__(self, start: float = 0.0):
        if not math.isfinite(start):
            raise ValueError("start must be finite")
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("A fake clock can only move forwards")
        self._now += seconds
        return self._now

    def set(self, now: float) -> float:
        if not math.isfinite(now) or now < self._now:
            raise ValueError("A fake clock can only move forwards")
        self._now = float(now)
        return self._now
