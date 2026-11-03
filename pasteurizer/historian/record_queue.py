"""Bounded hand-over queue between the control scan and the historian writer.

The control thread only appends (constant time, never blocks on storage).
The writer peeks a batch, stores it, and removes it only after the store
confirmed the write, so a failed batch is retried without being lost.

Memory is bounded by `capacity`. When the queue is full, the NEW record is
dropped and counted. Periodic samples may fill only `SAMPLE_SHARE` of the
queue, so a backlog of samples can never crowd out events.
"""

from collections import deque
import threading

EVENTS = "events"
SAMPLES = "samples"
SAMPLE_SHARE = 0.5


class RecordQueue:
    def __init__(self, capacity: int):
        if capacity < 2:
            raise ValueError("capacity must be at least 2")
        self.capacity = capacity
        self.sample_limit = max(1, int(capacity * SAMPLE_SHARE))
        self._items = deque()
        self._lock = threading.Lock()
        self._dropped = {EVENTS: 0, SAMPLES: 0}
        self._first_dropped_utc = None

    def put(self, record: dict) -> bool:
        table = record["table"]
        limit = self.sample_limit if table == SAMPLES else self.capacity
        with self._lock:
            if len(self._items) >= limit:
                self._dropped[table] += 1
                if self._first_dropped_utc is None:
                    self._first_dropped_utc = record["utc"]
                return False
            self._items.append(record)
            return True

    def peek(self, count: int) -> list:
        with self._lock:
            return [self._items[i] for i in range(min(count, len(self._items)))]

    def remove(self, count: int):
        with self._lock:
            for _ in range(min(count, len(self._items))):
                self._items.popleft()

    def __len__(self):
        with self._lock:
            return len(self._items)

    @property
    def drops_pending(self) -> bool:
        with self._lock:
            return any(self._dropped.values())

    @property
    def overflowing(self) -> bool:
        """Still too full to accept samples: drops are not over yet."""
        return len(self) >= self.sample_limit

    def take_drops(self):
        """(dropped events, dropped samples, UTC of the first drop); resets the counts."""
        with self._lock:
            result = (self._dropped[EVENTS], self._dropped[SAMPLES], self._first_dropped_utc)
            self._dropped = {EVENTS: 0, SAMPLES: 0}
            self._first_dropped_utc = None
            return result
