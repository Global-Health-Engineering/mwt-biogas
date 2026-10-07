"""Control valve signal conversion and position supervision.

This module calculates values only; it never writes to hardware.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ValveFeedback:
    raw: int
    status: int
    position_percent: float | None
    valid: bool
    error: str | None


def validate_endpoints(closed, opened):
    if closed is None or opened is None:
        raise ValueError("Valve endpoints are not configured")
    if not all(math.isfinite(value) for value in (closed, opened)):
        raise ValueError("Valve endpoints must be finite")
    if closed == opened:
        raise ValueError("Closed and open endpoints must differ")


def command_to_raw(position_percent, closed_raw, open_raw):
    """Convert a requested position to a raw command.

    Reversed signal direction is supported through reversed endpoints.
    """
    validate_endpoints(closed_raw, open_raw)

    if not math.isfinite(position_percent):
        raise ValueError("Requested position must be finite")
    if not 0.0 <= position_percent <= 100.0:
        raise ValueError("Requested position must be between 0 and 100")

    return round(
        closed_raw
        + position_percent / 100.0 * (open_raw - closed_raw)
    )


def process_feedback(raw, status, closed_raw, open_raw, margin_percent=2.0):
    raw = int(raw)
    status = int(status)

    if not math.isfinite(margin_percent) or margin_percent < 0:
        raise ValueError("Feedback margin must be finite and nonnegative")

    if status != 0:
        return ValveFeedback(
            raw, status, None, False,
            f"Feedback input status=0x{status:02X}",
        )

    if closed_raw is None or open_raw is None:
        return ValveFeedback(
            raw, status, None, False,
            "Feedback endpoints are not configured",
        )

    validate_endpoints(closed_raw, open_raw)

    position = 100.0 * (raw - closed_raw) / (open_raw - closed_raw)

    if not -margin_percent <= position <= 100.0 + margin_percent:
        return ValveFeedback(
            raw, status, None, False,
            f"Feedback outside configured range: {position:.1f}%",
        )

    # Preserve the calculated value; do not hide small endpoint deviations.
    return ValveFeedback(raw, status, position, True, None)


class ValveSupervisor:
    """Check whether feedback reaches a successfully issued target.

    Call set_target only after the hardware write succeeds.
    Repeating an unchanged target does not restart the timer.
    """

    def __init__(self, tolerance_percent=3.0, timeout_seconds=None):
        if not math.isfinite(tolerance_percent) or tolerance_percent < 0:
            raise ValueError("Position tolerance must be nonnegative")
        if timeout_seconds is not None:
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise ValueError("Movement timeout must be positive")

        self.tolerance = tolerance_percent
        self.timeout = timeout_seconds
        self.reset()

    def reset(self):
        self.target = None
        self._outside_since = None
        self._last_time = None

    def set_target(self, position_percent, now):
        if not math.isfinite(now):
            raise ValueError("Time must be finite")
        if not math.isfinite(position_percent):
            raise ValueError("Target must be finite")
        if not 0.0 <= position_percent <= 100.0:
            raise ValueError("Target must be between 0 and 100")

        if self.target != position_percent:
            self.target = position_percent
            self._outside_since = now

    def update(self, feedback, now):
        if not math.isfinite(now):
            raise ValueError("Time must be finite")
        if self._last_time is not None and now < self._last_time:
            raise ValueError("Time must not move backwards")
        self._last_time = now

        if not feedback.valid:
            return "FEEDBACK_UNAVAILABLE"
        if self.target is None:
            return "NO_TARGET"

        error = abs(feedback.position_percent - self.target)

        if error <= self.tolerance:
            self._outside_since = None
            return "AT_TARGET"

        if self._outside_since is None:
            self._outside_since = now

        if self.timeout is None:
            return "OUTSIDE_TOLERANCE_TIMEOUT_UNCONFIGURED"

        if now - self._outside_since >= self.timeout:
            return "POSITION_TIMEOUT"

        return "MOVING_OR_SETTLING"