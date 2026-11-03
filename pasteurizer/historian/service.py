"""Historian: the queue, the writer and its thread behind one small interface.

The event log (control thread) calls `submit`, `notices` and `degraded`.
Nothing here touches storage on the control thread, except
`previous_boot()`, which is read once before the scan starts.
"""

import time

from pasteurizer.historian.record_queue import RecordQueue
from pasteurizer.historian.writer import BackgroundWriter, HistorianWriter


class Historian:
    def __init__(self, store, settings, *, clock=time.monotonic, wall_clock=time.time,
                 clock_sync=lambda: None):
        self.store = store
        self.settings = settings
        self.queue = RecordQueue(settings.queue_capacity)
        self.writer = HistorianWriter(store, self.queue, settings, clock=clock,
                                      wall_clock=wall_clock, clock_sync=clock_sync)
        self._background: BackgroundWriter | None = None

    # Lifecycle -------------------------------------------------------------
    def start(self):
        """Start the writer thread (the RevPi and long laptop runs)."""
        if self._background is None:
            self._background = BackgroundWriter(self.writer, self.settings.flush_interval_s)
            self._background.start()

    def drain(self) -> int:
        """Simulation without a thread: store queued records now."""
        if self._background is not None:
            raise RuntimeError("drain() is for runs without the writer thread")
        return self.writer.drain()

    def close(self, timeout_s: float) -> int:
        """Flush and close. Returns the number of records NOT stored."""
        if self._background is not None:
            self._background.stop(timeout_s)
        else:
            self.writer.drain(force=True)
            self.writer.close()
        dropped_events, dropped_samples, _ = self.queue.take_drops()
        return len(self.queue) + dropped_events + dropped_samples

    def previous_boot(self, current_boot_id) -> dict | None:
        reader = getattr(self.store, "previous_boot", None)
        if reader is None:
            return None
        try:
            return reader(current_boot_id)
        except Exception:
            return None

    def unacknowledged_recoveries(self) -> list:
        """Recovery events after the last acknowledgement (startup read)."""
        reader = getattr(self.store, "unacknowledged_recoveries", None)
        if reader is None:
            return []
        try:
            return reader()
        except Exception:
            return []

    # Control-thread interface ---------------------------------------------
    def submit(self, record: dict) -> bool:
        return self.queue.put(record)

    @property
    def degraded(self) -> bool:
        """Storage failing or records dropped and not yet reported."""
        return not self.writer.healthy or self.queue.drops_pending

    def notices(self) -> list:
        """(kind, data) records to log once storage works again."""
        if not self.writer.healthy or (self.queue.drops_pending and self.queue.overflowing):
            return []           # gap not over yet: report it once, complete
        found = []
        outage = self.writer.take_recovered()
        drops = self.queue.take_drops()
        if outage is not None or any(drops[:2]):
            found.append(("LOG_GAP", {
                "cause": "STORAGE_UNAVAILABLE" if outage is not None else "QUEUE_OVERFLOW",
                "outage_s": None if outage is None else round(outage.duration_s, 3),
                "error": None if outage is None else outage.error,
                "dropped_events": drops[0],
                "dropped_samples": drops[1],
                "first_dropped_utc": drops[2],
            }))
        found.extend(self.writer.take_notices())
        return found

    def status(self) -> dict:
        """For the GUI status (memory only): health, backlog, disk usage."""
        return {"healthy": self.writer.healthy, "error": self.writer.error,
                "queued": len(self.queue), **self.writer.storage}
