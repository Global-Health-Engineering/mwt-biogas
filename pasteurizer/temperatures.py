"""Temperature conversion and channel-status handling."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TemperatureReading:
    raw: int | None
    status: int | None
    celsius: float | None
    valid: bool
    error: str | None
    offset_c: float = 0.0


def process_temperature(
    raw: int,
    status: int,
    plausible_min_c: float | None = None,
    plausible_max_c: float | None = None,
    offset_c: float = 0.0,
) -> TemperatureReading:
    """Convert an unscaled RevPi RTD reading to degrees Celsius.

    The optional plausible range describes what the process can physically
    produce at this sensor. A value outside it is reported invalid rather
    than passed on as a believable temperature.

    offset_c is the sensor's calibration correction, added to the converted
    value. The PT100 hardware range is checked on the uncorrected value; the
    plausible process range is checked on the corrected one.
    """

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
            offset_c=offset_c,
        )

    celsius = raw / 10.0

    if not -200.0 <= celsius <= 850.0:
        return TemperatureReading(
            raw=raw,
            status=status,
            celsius=None,
            valid=False,
            error="temperature outside PT100 range",
            offset_c=offset_c,
        )

    celsius = round(celsius + offset_c, 2)

    if (
        (plausible_min_c is not None and celsius < plausible_min_c)
        or (plausible_max_c is not None and celsius > plausible_max_c)
    ):
        return TemperatureReading(
            raw=raw,
            status=status,
            celsius=None,
            valid=False,
            error=(
                f"{celsius:.1f} C outside plausible range "
                f"{plausible_min_c}..{plausible_max_c} C"
            ),
            offset_c=offset_c,
        )

    return TemperatureReading(
        raw=raw,
        status=status,
        celsius=celsius,
        valid=True,
        error=None,
        offset_c=offset_c,
    )
