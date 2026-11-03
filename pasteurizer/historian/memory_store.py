"""In-memory historian store for simulation and scenario tests.

`fail` simulates unavailable storage, `delay_s` slow storage. Like the
SQLite store it ignores a record it already holds (same boot and sequence).
"""

import time


class MemoryHistorianStore:
    def __init__(self):
        self.events: list[dict] = []
        self.samples: list[dict] = []
        self.boots: list[dict] = []
        self.fail = False
        self.delay_s = 0.0
        self.writes = 0
        self._keys = set()

    @property
    def records(self) -> list[dict]:
        return self.events

    def write(self, records):
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise OSError("Simulated historian storage failure")
        self.writes += 1
        for r in records:
            key = (r["boot_id"], r["sequence"])
            if key in self._keys:
                continue
            self._keys.add(key)
            (self.events if r["table"] == "events" else self.samples).append(r)
            self._boot(r)

    def _boot(self, r):
        boot = next((b for b in self.boots if b["boot_id"] == r["boot_id"]), None)
        if boot is None:
            boot = {"boot_id": r["boot_id"], "started_utc": None, "config_hash": None,
                    "file_hashes": None, "last_utc": None, "stopped_utc": None,
                    "stop_reason": None}
            self.boots.append(boot)
        if r["table"] == "events" and r["kind"] == "CONTROLLER_START":
            boot.update(started_utc=r["utc"], config_hash=r["data"].get("config_hash"),
                        file_hashes=r["data"].get("file_hashes"))
        if r["table"] == "events" and r["kind"] == "CONTROLLER_STOP":
            boot.update(stopped_utc=r["utc"], stop_reason=r["data"].get("reason"))
        boot["last_utc"] = r["utc"]

    def previous_boot(self, current_boot_id):
        others = [b for b in self.boots if b["boot_id"] != current_boot_id]
        return dict(others[-1]) if others else None

    def unacknowledged_recoveries(self):
        found = []
        for event in reversed(self.events):
            if event["kind"] == "HISTORIAN_RECOVERY_ACKNOWLEDGED":
                break
            if event["kind"] == "HISTORIAN_DB_RECOVERED":
                found.insert(0, event)
        return found

    def prune(self, now_epoch, *, clock_trusted):
        return {"expired_events": 0, "expired_samples": 0, "size_events": 0,
                "size_samples": 0, "database_mb": 0.0, "corrupt_files": [],
                "corrupt_mb": 0.0}
