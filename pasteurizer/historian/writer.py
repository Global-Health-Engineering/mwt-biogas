"""Moves queued records into the store, outside the control scan.

HistorianWriter does the work; BackgroundWriter runs it in its own thread.
Simulations without a thread call `drain()` between scan cycles instead.

On a store error the writer becomes unhealthy, keeps the batch queued and
retries every `retry_interval_s`. When a write succeeds again it remembers
the outage, which the event log turns into a LOG_GAP record. Retention and
the size limit are applied at the first drain and then every
PRUNE_INTERVAL_S; each run is reported as HISTORIAN_STORAGE (deleted counts,
database size, preserved corrupt files and their size, separately).

Only this thread touches the store or the file system. The control thread
reads the results (health, notices, the last storage report) from memory.
"""

from dataclasses import dataclass
import threading
import time

PRUNE_INTERVAL_S = 3600.0


@dataclass(frozen=True)
class Outage:
    started_mono: float
    ended_mono: float
    error: str

    @property
    def duration_s(self) -> float:
        return self.ended_mono - self.started_mono


class HistorianWriter:
    def __init__(self, store, queue, settings, *, clock=time.monotonic,
                 wall_clock=time.time, clock_sync=lambda: None):
        self.store = store
        self.queue = queue
        self.batch_size = settings.write_batch_size
        self.retry_interval_s = settings.retry_interval_s
        self.clock = clock
        self.wall_clock = wall_clock
        self.clock_sync = clock_sync
        self._lock = threading.Lock()
        self._healthy = True
        self._error = None
        self._failed_since = None
        self._next_retry = None
        self._recovered: Outage | None = None
        self._notices: list = []
        self._storage: dict = {}
        self._next_prune = None

    # Health (read by the control thread) --------------------------------
    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def take_recovered(self) -> Outage | None:
        with self._lock:
            outage, self._recovered = self._recovered, None
            return outage

    def take_notices(self) -> list:
        with self._lock:
            notices, self._notices = self._notices, []
            return notices

    @property
    def storage(self) -> dict:
        """Last storage report (from memory; no file-system access)."""
        with self._lock:
            return dict(self._storage)

    def prune_soon(self):
        """Apply retention and the size limit on the next drain."""
        self._next_prune = None

    # Work (writer thread only) -------------------------------------------
    def drain(self, *, force=False) -> int:
        """Write everything queued; returns the number of records stored."""
        now = self.clock()
        if not force and self._next_retry is not None and now < self._next_retry:
            return 0
        written = 0
        while True:
            batch = self.queue.peek(self.batch_size)
            if not batch:
                break
            try:
                self.store.write(batch)
            except Exception as error:      # any storage problem, never fatal
                self._failed(error, now)
                return written
            self.queue.remove(len(batch))
            written += len(batch)
            self._succeeded(now)
            self._collect_store_notices()
        self._maintain(now)
        return written

    def _collect_store_notices(self):
        take = getattr(self.store, "take_notices", None)
        if take is None:
            return
        notices = take()
        if notices:
            with self._lock:
                self._notices.extend(notices)

    def _failed(self, error, now):
        reset = getattr(self.store, "reset", None)
        if reset is not None:
            try:
                reset()
            except Exception:
                pass
        with self._lock:
            if self._healthy:
                self._failed_since = now
            self._healthy = False
            self._error = f"{type(error).__name__}: {error}"
            self._next_retry = now + self.retry_interval_s

    def _succeeded(self, now):
        with self._lock:
            if not self._healthy:
                self._recovered = Outage(self._failed_since, now, self._error)
            self._healthy = True
            self._error = None
            self._failed_since = None
            self._next_retry = None

    def _maintain(self, now):
        if self._next_prune is not None and now < self._next_prune:
            return
        self._next_prune = now + PRUNE_INTERVAL_S
        prune = getattr(self.store, "prune", None)
        if prune is None:
            return
        try:
            result = prune(self.wall_clock(), clock_trusted=self.clock_sync() is not False)
        except Exception as error:
            self._failed(error, now)
            return
        self._collect_store_notices()
        with self._lock:
            self._storage = dict(result)
            self._notices.append(("HISTORIAN_STORAGE", dict(result)))

    def close(self):
        close = getattr(self.store, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


class BackgroundWriter:
    """Runs a HistorianWriter in a daemon thread every `flush_interval_s`."""

    def __init__(self, writer: HistorianWriter, flush_interval_s: float):
        self.writer = writer
        self.interval = flush_interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="historian", daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            self._drain()
        self._drain(force=True)             # final flush on orderly shutdown
        self.writer.close()

    def _drain(self, force=False):
        try:
            self.writer.drain(force=force)
        except Exception as error:          # a bug must not end the thread silently
            self.writer._failed(error, self.writer.clock())

    def stop(self, timeout_s: float) -> bool:
        """True when the thread finished its final flush within the timeout."""
        self._stop.set()
        self._thread.join(timeout_s)
        return not self._thread.is_alive()
