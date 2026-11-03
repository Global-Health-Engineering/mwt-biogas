"""Overflow relief valves Y1/Y2 (spec section 10).

Open when the tank's overflow switch is reached. Close only when both hold:
open for at least min_open_s since opening, and the switch clear
continuously for close_after_clear_s (a new detection restarts that timer).
With an unknown overflow level the valve closes to its power-loss position;
drainage is then possible by hand through the manual bypass valves.
"""


class OverflowValve:
    def __init__(self, settings):
        self.s = settings
        self.open = False
        self.opened_at = None
        self.clear_since = None

    def update(self, overflow: bool | None, now: float) -> bool:
        if overflow is None:
            self.open = False
            self.opened_at = None
            self.clear_since = None
            return False

        if overflow:
            if not self.open:
                self.open = True
                self.opened_at = now
            self.clear_since = None
            return True

        if self.open:
            if self.clear_since is None:
                self.clear_since = now
            if (now - self.opened_at >= self.s.min_open_s
                    and now - self.clear_since >= self.s.close_after_clear_s):
                self.open = False
                self.opened_at = None
                self.clear_since = None
        return self.open
