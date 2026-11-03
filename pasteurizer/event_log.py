"""Timestamped event log and process-data samples (spec section 13).

Every record carries its data source (LIVE plant I/O or SIMULATED), its
clock source (SYSTEM clock or SIMULATED time), UTC time, the clock-sync
state, monotonic time, boot ID and a sequence number (shared by events and
samples). Control timing never
uses the wall clock. A wall-clock jump against monotonic time is recorded as
CLOCK_CORRECTION (old time, new time, size); a change of the sync state as
CLOCK_SYNC_CHANGED.

Records are only stamped and queued here; the historian stores them outside
the control scan. `record` and `sample` never raise and never block. While
storage fails or records are dropped, `check_health()` returns True (the
controller raises the self-clearing LOGGER_FAILURE warning). Once storage
works again a LOG_GAP record states the outage and the dropped-record counts.

After a corrupt historian database was preserved and replaced,
`recovery_pending` lists every recovery not yet acknowledged (the yellow
HISTORIAN_DB_RECOVERED warning). One acknowledgement covers them all, and its
HISTORIAN_RECOVERY_ACKNOWLEDGED record lists each recovery event and
preserved file. The list survives a restart: each recovery event also
carries the recoveries still unacknowledged before it, because those earlier
events may be inside a file that has since been preserved itself.
"""

from datetime import datetime, timezone
import threading
import time
import uuid

from pasteurizer.clock import Clock, MonotonicClock
from pasteurizer.historian import Historian, MemoryHistorianStore

CLOCK_JUMP_THRESHOLD_S = 1.0
SYNC_STATES = {True: "SYNCHRONISED", False: "UNSYNCHRONISED", None: "UNKNOWN"}
DATA_SOURCES = ("LIVE", "SIMULATED")


class EventLog:
    def __init__(self, historian: Historian | None = None, *, clock: Clock | None = None,
                 wall_clock=time.time, clock_sync=lambda: None):
        self.clock = clock if clock is not None else MonotonicClock()
        self.wall_clock = wall_clock
        self.clock_sync = clock_sync
        if historian is None:       # desk checks: memory only, own writer thread
            historian = Historian(MemoryHistorianStore(), _DefaultSettings)
            historian.start()
        self.historian = historian
        self.boot_id = uuid.uuid4().hex
        self.sequence = 0
        self._last = None          # (monotonic, wall) of the previous record
        self._sync = None          # last clock-sync state seen
        self._lock = threading.RLock()   # a future GUI thread may log too
        self.failed = False
        # Set by the controller from its reader; UNKNOWN until then.
        self.data_source = "UNKNOWN"
        system_time = isinstance(self.clock, MonotonicClock) and wall_clock is time.time
        self.clock_source = "SYSTEM" if system_time else "SIMULATED"
        self.recovery_pending: list[dict] = []

    # Public ------------------------------------------------------------------
    def record(self, kind: str, **data) -> dict:
        return self._submit("events", kind, data)

    def sample(self, **data) -> dict:
        return self._submit("samples", None, data)

    def check_health(self) -> bool:
        """Once per cycle: clock checks, gap/prune notices; True while degraded."""
        with self._lock:
            try:
                mono, wall, sync = self.clock.monotonic(), self.wall_clock(), self.clock_sync()
                for extra in self._clock_events(mono, wall, sync):
                    self.historian.submit(extra)
                for kind, data in self.historian.notices():
                    if kind == "HISTORIAN_DB_RECOVERED":
                        self._recovery(data)
                    else:
                        self.record(kind, **data)
                self.failed = self.historian.degraded
            except Exception:
                self.failed = True
            return self.failed

    @property
    def sync_state(self) -> str:
        """Clock-sync state as last seen (SYNCHRONISED / UNSYNCHRONISED / UNKNOWN)."""
        return SYNC_STATES.get(self._sync, "UNKNOWN")

    def previous_boot(self) -> dict | None:
        """The last run recorded before this one (read once at startup)."""
        return self.historian.previous_boot(self.boot_id)

    def restore_recovery_warning(self):
        """At startup: recoveries not yet acknowledged stay pending."""
        entries = {}
        for event in self.historian.unacknowledged_recoveries():
            for entry in event["data"].get("unacknowledged_before", []) + [_entry(event)]:
                entries[(entry["boot_id"], entry["sequence"])] = entry
        self.recovery_pending = sorted(entries.values(), key=lambda e: e["utc"])

    def acknowledge_recovery(self, user) -> bool:
        """Clears the warning for every pending recovery; preserved files are kept."""
        with self._lock:
            pending, self.recovery_pending = self.recovery_pending, []
            if not pending:
                return False
            self.record("HISTORIAN_RECOVERY_ACKNOWLEDGED", user=getattr(user, "name", None),
                        recoveries=pending, count=len(pending),
                        preserved_files=[e["preserved_as"] for e in pending],
                        note="Preserved corrupt files are kept")
        return True

    def _recovery(self, data):
        record = self.record("HISTORIAN_DB_RECOVERED", **data,
                             unacknowledged_before=list(self.recovery_pending))
        if record:
            self.recovery_pending.append(_entry(record))

    def close(self, timeout_s: float = 5.0) -> int:
        """Flush and stop the historian; returns the records NOT stored."""
        return self.historian.close(timeout_s)

    # Internal ------------------------------------------------------------------
    def _submit(self, table, kind, data) -> dict:
        with self._lock:
            return self._stamp_and_queue(table, kind, data)

    def _stamp_and_queue(self, table, kind, data) -> dict:
        try:
            mono = self.clock.monotonic()
            wall = self.wall_clock()
            sync = self.clock_sync()
            for extra in self._clock_events(mono, wall, sync):
                self.historian.submit(extra)
            record = self._stamp(table, kind, data, mono, wall, sync)
            self.historian.submit(record)
            return record
        except Exception:           # logging must never stop the control scan
            self.failed = True
            return {}

    def _clock_events(self, mono, wall, sync):
        events = []
        if self._last is not None:
            expected = self._last[1] + (mono - self._last[0])
            if abs(wall - expected) > CLOCK_JUMP_THRESHOLD_S:
                events.append(self._stamp("events", "CLOCK_CORRECTION", {
                    "old_utc": _iso(expected),
                    "new_utc": _iso(wall),
                    "correction_s": round(wall - expected, 3),
                }, mono, wall, sync))
        self._last = (mono, wall)
        if self.sequence and sync != self._sync:
            events.append(self._stamp("events", "CLOCK_SYNC_CHANGED", {
                "old": SYNC_STATES.get(self._sync, "UNKNOWN"),
                "new": SYNC_STATES.get(sync, "UNKNOWN")}, mono, wall, sync))
        self._sync = sync
        return events

    def _stamp(self, table, kind, data, mono, wall, sync) -> dict:
        self.sequence += 1
        record = {
            "table": table,
            "source": self.data_source,
            "clock_source": self.clock_source,
            "utc": _iso(wall),
            "utc_epoch": wall,
            "clock_sync": SYNC_STATES.get(sync, "UNKNOWN"),
            "utc_synchronised": sync is True,
            "monotonic_s": round(mono, 6),
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "data": data,
        }
        if kind is not None:
            record["kind"] = kind
        return record


def _entry(record) -> dict:
    """Identifies one recovery event: when, which run, sequence, preserved file."""
    return {"utc": record["utc"], "boot_id": record["boot_id"],
            "sequence": record["sequence"],
            "preserved_as": record["data"].get("preserved_as"),
            "error": record["data"].get("error")}


def _iso(epoch) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class _DefaultSettings:
    """Used only when no historian is given (desk checks): memory, no thread."""
    queue_capacity = 10000
    write_batch_size = 500
    retry_interval_s = 1.0
    flush_interval_s = 1.0
