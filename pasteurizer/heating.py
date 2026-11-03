"""1 kW assist heater with hysteresis and minimum switching times (spec section 6).

The 3 kW preheat stage and the circulation pump are decided directly by the
plant state machine; this module only owns the 1 kW switching behaviour.
"""

import math


class AssistHeater:
    def __init__(self, settings):
        self.settings = settings
        self.reset()

    def reset(self):
        self.on = False
        self.on_since = None
        self.off_since = None

    def update(self, pt5_c, on_below_c, off_at_c, permitted: bool, now: float) -> bool:
        """Return the 1 kW request.

        Withdrawn permission (stop, critical fault, invalid temperature,
        overtemperature) switches off at once, overriding the minimum ON time.
        """
        valid = (isinstance(pt5_c, (int, float)) and not isinstance(pt5_c, bool)
                 and math.isfinite(pt5_c))
        if not permitted or not valid:
            if self.on:
                self.on = False
                self.off_since = now
            return False

        s = self.settings
        if self.on:
            if pt5_c >= off_at_c and now - self.on_since >= s.min_on_s:
                self.on = False
                self.off_since = now
        else:
            off_long_enough = self.off_since is None or now - self.off_since >= s.min_off_s
            if pt5_c < on_below_c and off_long_enough:
                self.on = True
                self.on_since = now
        return self.on
