"""Closed-valve protection of the effluent feed pump GPST-PU-02.

The pump has no running feedback; this works on the written frames. "Closed"
means Y3 not written open, or Y4 feedback invalid or at or below the closed
tolerance (start.y4_closed_tolerance_percent). When the pump has been written
on against closed valves for feed_pump.closed_valve_max_s, it is cut off.

Not latching: the cutoff (and its Level 1 warning FEED_PUMP_CLOSED_VALVES)
lasts until the pump is requested anew - in AUTO with the next treatment
start, in MANUAL with a new press of the pump request. The normal start (Y3
open, pump on, Y4 opening) and the stop run-on stay well inside the limit.
"""

from dataclasses import dataclass

from pasteurizer.fault_manager import Persistence

FEED_PUMP = "effluent_feed_pump"


@dataclass(frozen=True)
class FeedPumpCheck:
    running: bool               # written on in the last accepted frame
    against_closed_s: float     # time on against closed valves (0 when not)
    cut_off: bool
    reason: str


class FeedPumpGuard:
    def __init__(self, control_settings):
        self.max_s = control_settings.feed_pump.closed_valve_max_s
        self.tolerance = control_settings.start.y4_closed_tolerance_percent
        self.timer = Persistence()
        self.cut_off = False
        self.cut_off_at: float | None = None
        self._requested = False
        self.last = FeedPumpCheck(False, 0.0, False, "Feed pump off")

    def request_edge(self, requested: bool):
        """A new request (False -> True) re-arms the pump after a cutoff."""
        if requested and not self._requested and self.cut_off:
            self.cut_off = False
            self.cut_off_at = None
            self.timer.reset()
        self._requested = requested

    def closed(self, inputs, last_written) -> bool:
        if last_written is None or not last_written.pasteurizer_isolation_open:
            return True
        return not inputs.y4_feedback.valid or inputs.y4_closed(self.tolerance)

    def update(self, inputs, last_written) -> FeedPumpCheck:
        now = inputs.now
        running = bool(last_written is not None and getattr(last_written, FEED_PUMP))
        against = running and self.closed(inputs, last_written)
        held = self.timer.update(against, now)
        if against and held >= self.max_s and not self.cut_off:
            self.cut_off = True
            self.cut_off_at = now
        if self.cut_off:
            reason = (f"Feed pump cut off: ran {self.max_s:g} s against a closed Y3 or Y4; "
                      "needs a new request")
        elif against:
            reason = f"Feed pump on against closed valves for {held:.1f} s (limit {self.max_s:g} s)"
        else:
            reason = "Feed pump running" if running else "Feed pump off"
        self.last = FeedPumpCheck(running, held if against else 0.0, self.cut_off, reason)
        return self.last
