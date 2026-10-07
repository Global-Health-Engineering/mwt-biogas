"""Float-switch polarity and time-based debounce."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class LevelReading:
    raw: bool
    reached: bool | None
    pending: bool


class LevelProcessor:
    def __init__(self, active_high=True, debounce_seconds=0.2):
        if not isinstance(active_high, bool):
            raise ValueError("active_high must be a boolean")
        if (
            not math.isfinite(debounce_seconds)
            or debounce_seconds < 0
        ):
            raise ValueError("Debounce must be finite and non-negative")

        self.active_high = active_high
        self.debounce_seconds = debounce_seconds
        self.reset()

    def reset(self):
        """Discard previous state after startup or lost communication."""
        self._candidate = None
        self._candidate_since = None
        self._accepted = None

    def update(self, raw: bool, now: float) -> LevelReading:
        """Call regularly using time.monotonic() for now."""
        raw = bool(raw)
        candidate = raw if self.active_high else not raw

        if candidate != self._candidate:
            self._candidate = candidate
            self._candidate_since = now

        if now - self._candidate_since >= self.debounce_seconds:
            self._accepted = candidate

        return LevelReading(
            raw=raw,
            reached=self._accepted,
            pending=self._accepted != candidate,
        )