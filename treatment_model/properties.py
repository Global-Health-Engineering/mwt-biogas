"""Approximate liquid-water properties for preliminary hydraulics.

Rounded water-property table, linearly interpolated.
Restricted to 10–90 °C; no extrapolation.
Replace with the selected IAPWS implementation for the final model.
"""

import math

# Temperature °C, density kg/m³, dynamic viscosity Pa·s.
_TABLE = (
    (10.0, 999.7, 1.306e-3),
    (20.0, 998.2, 1.002e-3),
    (30.0, 995.7, 0.797e-3),
    (40.0, 992.2, 0.653e-3),
    (50.0, 988.0, 0.547e-3),
    (60.0, 983.2, 0.466e-3),
    (70.0, 977.8, 0.404e-3),
    (80.0, 971.8, 0.355e-3),
    (90.0, 965.3, 0.315e-3),
)


def water_properties(temperature_c):
    t = float(temperature_c)
    if not math.isfinite(t) or not 10.0 <= t <= 90.0:
        raise ValueError("Water-property temperature must be 10–90 °C")

    for lower, upper in zip(_TABLE, _TABLE[1:]):
        if lower[0] <= t <= upper[0]:
            fraction = (t - lower[0]) / (upper[0] - lower[0])
            density = lower[1] + fraction * (upper[1] - lower[1])
            viscosity = lower[2] + fraction * (upper[2] - lower[2])
            return density, viscosity

    raise ValueError("Temperature interpolation failed")
