"""Controller scenarios grouped by topic (control spec section 16).

Importing the topic modules registers their scenarios in SCENARIOS.
"""

from scenarios import (  # noqa: F401  (imported for registration)
    equipment,
    faults,
    gui,
    gui_data,
    historian,
    manual,
    operator,
    shadow,
    startup_heating,
    treatment,
)
from scenarios.harness import SCENARIOS

__all__ = ["SCENARIOS"]
