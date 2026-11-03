"""Link to the controller's status publisher, with connection and staleness.

Polled once per second. The data counts as live only while NEW snapshots
arrive (a new boot_id or cycle number): if none arrives for `stale_after_s`
(2 s), the state is STALE while the publisher still answers, DISCONNECTED
when it does not. The GUI then greys every live value.
"""

import json
import threading
import time
import urllib.request

STATES = ("WAITING", "CONNECTED", "STALE", "DISCONNECTED")


def http_fetch(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode())


class ControllerLink:
    def __init__(self, url="http://127.0.0.1:8081", *, stale_after_s=2.0, poll_s=1.0,
                 clock=time.monotonic, fetch=http_fetch):
        self.url = url.rstrip("/")
        self.stale_after_s = stale_after_s
        self.poll_s = poll_s
        self.clock = clock
        self.fetch = fetch
        self._lock = threading.Lock()
        self._status = None
        self._config = None
        self._key = None
        self._last_new = None
        self._responding = False
        self._ever_responded = False
        self._error = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="controller-link", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_s)

    def poll_once(self):
        try:
            status = self.fetch(self.url + "/status", min(0.8, self.poll_s))
            config = None
            key = (status.get("boot_id"), status.get("cycle"))
            if self._config is None or key[0] != (self._key or (None,))[0]:
                config = self.fetch(self.url + "/config", min(0.8, self.poll_s))
        except Exception as error:      # connection refused, timeout, 503, bad JSON
            with self._lock:
                self._responding = False
                self._error = f"{type(error).__name__}: {error}"
            return
        now = self.clock()
        with self._lock:
            self._responding = self._ever_responded = True
            self._error = None
            if config is not None:
                self._config = config
            if key != self._key:
                self._key = key
                self._last_new = now
                self._status = status

    def view(self) -> dict:
        now = self.clock()
        with self._lock:
            age = None if self._last_new is None else round(now - self._last_new, 2)
            if self._status is None:
                state = "WAITING" if not self._ever_responded else "STALE"
                if self._error and not self._responding:
                    state = "DISCONNECTED"
            elif age < self.stale_after_s:
                state = "CONNECTED"
            else:
                state = "STALE" if self._responding else "DISCONNECTED"
            return {"state": state, "age_s": age, "responding": self._responding,
                    "error": self._error, "stale_after_s": self.stale_after_s,
                    "status": self._status, "config": self._config}
