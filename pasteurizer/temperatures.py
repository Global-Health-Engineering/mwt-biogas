"""Temperature conversion and channel-status handling."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TemperatureReading:
    raw: int
    status: int
    celsius: float | None
    valid: bool
    error: str | None


def process_temperature(raw: int, status: int) -> TemperatureReading:
    """Convert an unscaled RevPi RTD reading to degrees Celsius."""

    raw = int(raw)
    status = int(status)

    if status != 0:
        errors = []

        if status & 1:
            errors.append("below range or sensor short circuit")
        if status & 2:
            errors.append("above range or sensor/cable disconnected")
        if status & ~3:
            errors.append(f"other status bits: 0x{status:02x}")

        return TemperatureReading(
            raw=raw,
            status=status,
            celsius=None,
            valid=False,
            error="; ".join(errors),
        )

    celsius = raw / 10.0

    if not -200.0 <= celsius <= 850.0:
        return TemperatureReading(
            raw=raw,
            status=status,
            celsius=None,
            valid=False,
            error="temperature outside PT100 range",
        )

    return TemperatureReading(
        raw=raw,
        status=status,
        celsius=celsius,
        valid=True,
        error=None,
    )