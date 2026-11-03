"""Controller heartbeat and watchdog (spec section 13).

The controller sends a heartbeat only after a control cycle AND its output
write completed successfully. The watchdog must be independent of the
controller process; the real mechanism (RevPi hardware watchdog and module
behaviour when process-image updates stop) is still UNASSIGNED.

SoftwareWatchdog is the simulation stand-in: when no heartbeat arrives for
the timeout it shuts the process outputs down and marks the persistent state,
so that the restarted controller latches WATCHDOG_RESTART.
"""


class SoftwareWatchdog:
    def __init__(self, timeout_s: float, on_trip):
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.timeout_s = timeout_s
        self.on_trip = on_trip
        self.last_heartbeat = None
        self.tripped = False

    def heartbeat(self, now: float):
        if not self.tripped:
            self.last_heartbeat = now

    def check(self, now: float) -> bool:
        """Call independently of the controller; returns True once tripped."""
        if self.tripped or self.last_heartbeat is None:
            return self.tripped
        if now - self.last_heartbeat >= self.timeout_s:
            self.tripped = True
            self.on_trip()
        return self.tripped
