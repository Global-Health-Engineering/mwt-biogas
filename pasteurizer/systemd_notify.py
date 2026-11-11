"""systemd service notifications (Type=notify, WatchdogSec) without dependencies.

When the controller runs under deploy/mwt-biogas-controller.service, systemd
sets NOTIFY_SOCKET. The runner then reports READY=1 after its first complete
scan, WATCHDOG=1 after every scan whose output write succeeded, and STOPPING=1
at the start of its shutdown. If the WATCHDOG=1 messages stop (the whole
process hangs, so the in-process watchdog cannot act either), systemd kills
the process after WatchdogSec; ExecStopPost (tools/safe_outputs.py) then
switches every output off and records the abnormal stop.

Outside systemd (no NOTIFY_SOCKET) every call does nothing.
"""

import os
import socket
import time


class SystemdNotifier:
    def __init__(self, environ=None, min_interval_s=0.5, clock=time.monotonic):
        environ = os.environ if environ is None else environ
        address = environ.get("NOTIFY_SOCKET") or None
        if address and address.startswith("@"):
            address = "\0" + address[1:]         # abstract namespace socket
        self.address = address
        self.min_interval_s = min_interval_s
        self.clock = clock
        self._last_watchdog = None
        self._socket = None
        self.errors = 0
        watchdog_usec = environ.get("WATCHDOG_USEC")
        self.watchdog_s = int(watchdog_usec) / 1e6 if watchdog_usec and watchdog_usec.isdigit() else None

    @property
    def active(self) -> bool:
        return self.address is not None and hasattr(socket, "AF_UNIX")

    def _send(self, message: str) -> bool:
        if not self.active:
            return False
        try:
            if self._socket is None:
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._socket.sendto(message.encode(), self.address)
            return True
        except OSError:
            self.errors += 1                     # never stops the scan
            return False

    def ready(self, status: str = "") -> bool:
        return self._send("READY=1" + (f"\nSTATUS={status}" if status else ""))

    def watchdog(self) -> bool:
        """Call only after a scan whose output write succeeded (rate limited)."""
        now = self.clock()
        if self._last_watchdog is not None and now - self._last_watchdog < self.min_interval_s:
            return False
        self._last_watchdog = now
        return self._send("WATCHDOG=1")

    def stopping(self, status: str = "") -> bool:
        return self._send("STOPPING=1" + (f"\nSTATUS={status}" if status else ""))

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
