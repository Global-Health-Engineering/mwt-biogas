"""Is the wall clock synchronised? (spec section 13)

On Linux the kernel keeps the NTP state; adjtimex(2) with no changes returns
TIME_ERROR (5) while the clock is unsynchronised. This is one cheap system
call, and the answer is cached for `refresh_s`. Elsewhere (a Windows laptop)
the state is unknown: None.

Timers never use the wall clock; this only flags timestamps.
"""

import ctypes
import sys
import time

TIME_ERROR = 5


def _adjtimex():
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True).adjtimex
    except (OSError, AttributeError):
        return None


class KernelClockSync:
    def __init__(self, refresh_s: float = 10.0, clock=time.monotonic):
        self.refresh_s = refresh_s
        self.clock = clock
        self._call = _adjtimex()
        self._value = None
        self._checked_at = None

    def __call__(self) -> bool | None:
        now = self.clock()
        if self._checked_at is None or now - self._checked_at >= self.refresh_s:
            self._value = self._query()
            self._checked_at = now
        return self._value

    def _query(self) -> bool | None:
        if self._call is None:
            return None
        buffer = ctypes.create_string_buffer(512)   # struct timex, modes = 0
        try:
            state = self._call(buffer)
        except OSError:
            return None
        if state < 0:
            return None
        return state != TIME_ERROR
