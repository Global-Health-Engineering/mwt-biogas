"""Small numeric helpers shared by several modules."""

import math


def is_finite_number(value) -> bool:
    """True for int/float values that are finite (bool excluded)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))
