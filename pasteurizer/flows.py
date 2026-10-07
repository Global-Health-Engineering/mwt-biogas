"""Process cumulative 32-bit flow counters without writing to hardware."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class FlowReading:
    raw_counter: int
    pulse_rate_hz: float | None
    flow_lpm: float | None
    measured_volume_litres: float | None
    valid: bool
    calibrated: bool
    error: str | None


class FlowProcessor:
    def __init__(
        self,
        pulses_per_litre=None,
        calibration_confirmed=False,
        max_pulse_rate_hz=None,
        max_sample_gap_seconds=3.0,
    ):
        if pulses_per_litre is not None:
            if not math.isfinite(pulses_per_litre) or pulses_per_litre <= 0:
                raise ValueError("pulses_per_litre must be positive")

        if max_pulse_rate_hz is not None:
            if not math.isfinite(max_pulse_rate_hz) or max_pulse_rate_hz <= 0:
                raise ValueError("max_pulse_rate_hz must be positive")

        if not math.isfinite(max_sample_gap_seconds):
            raise ValueError("max_sample_gap_seconds must be finite")
        if max_sample_gap_seconds <= 0:
            raise ValueError("max_sample_gap_seconds must be positive")

        if not isinstance(calibration_confirmed, bool):
            raise ValueError("calibration_confirmed must be a boolean")
        if calibration_confirmed and pulses_per_litre is None:
            raise ValueError("Confirmed calibration needs pulses_per_litre")

        self.pulses_per_litre = pulses_per_litre
        self.calibrated = calibration_confirmed
        self.max_pulse_rate_hz = max_pulse_rate_hz
        self.max_sample_gap_seconds = max_sample_gap_seconds

        self._previous_counter = None
        self._previous_time = None
        self._measured_pulses = 0

    def reset(self):
        """Discard the sampling baseline, retaining measured session volume."""
        self._previous_counter = None
        self._previous_time = None

    def _reading(self, raw, rate=None, valid=False, error=None):
        factor = self.pulses_per_litre

        return FlowReading(
            raw_counter=raw,
            pulse_rate_hz=rate,
            flow_lpm=None if factor is None or rate is None
            else rate * 60.0 / factor,
            measured_volume_litres=None if factor is None
            else self._measured_pulses / factor,
            valid=valid,
            calibrated=self.calibrated,
            error=error,
        )

    def update(self, raw_counter, now):
        if not math.isfinite(now):
            raise ValueError("Sampling time must be finite")

        raw = int(raw_counter)
        counter = raw & 0xFFFFFFFF

        if self._previous_counter is None:
            self._previous_counter = counter
            self._previous_time = now
            return self._reading(raw, error="Waiting for second sample")

        elapsed = now - self._previous_time
        if elapsed <= 0:
            self.reset()
            return self._reading(raw, error="Invalid sampling interval")

        previous = self._previous_counter
        self._previous_counter = counter
        self._previous_time = now

        if elapsed > self.max_sample_gap_seconds:
            return self._reading(raw, error="Sampling gap; interval discarded")

        # Modular subtraction handles signed representation and rollover.
        pulses = (counter - previous) & 0xFFFFFFFF

        # A large backwards jump is treated as a reset/discontinuity.
        if pulses >= 0x80000000:
            return self._reading(raw, error="Counter reset or discontinuity")

        rate = pulses / elapsed

        if self.max_pulse_rate_hz is not None:
            # Allow one pulse for interval-boundary quantisation.
            if pulses > self.max_pulse_rate_hz * elapsed + 1:
                return self._reading(raw, error="Pulse rate exceeds limit")

        self._measured_pulses += pulses
        return self._reading(raw, rate=rate, valid=True)