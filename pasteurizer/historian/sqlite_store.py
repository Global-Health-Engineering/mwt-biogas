"""Local SQLite historian database.

Tables
  events   one row per event record (kind + JSON data)
  samples  one row per periodic process-data sample (zlib-compressed JSON,
           about 7x smaller; read it with `decode_sample` or the CSV export)
  boots    one row per controller run: start, config hash, last write, clean stop

Every row carries UTC time, clock-sync state, monotonic time, boot ID and the
sequence number. Events and samples share one sequence per boot, so a gap
in the sequence shows exactly where records were lost.

The connection is opened lazily by the writer thread. WAL mode lets the CSV
export read while the controller writes. `synchronous=FULL` makes every
committed batch durable before the next one is taken from the queue.

The first connection runs `PRAGMA quick_check`. A corrupt database (at that
check or on any later write) is preserved under a new name and replaced by
a fresh one; the failed batch is then written to the fresh database, so no
queued record is lost (see quarantine.py).
"""

import json
import math
from pathlib import Path
import sqlite3
import time
import zlib

from pasteurizer.historian.quarantine import is_corruption, preserve, storage_report

SCHEMA_VERSION = 2     # 2: source and clock_source columns
_COLUMNS = "source, clock_source, boot_id, sequence, utc, utc_epoch, clock_sync, monotonic_s"
# Added in version 2; rows written before keep UNKNOWN.
_SOURCE_COLUMNS = ("source", "clock_source")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    utc TEXT NOT NULL, utc_epoch REAL NOT NULL, clock_sync TEXT NOT NULL,
    monotonic_s REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'UNKNOWN', clock_source TEXT NOT NULL DEFAULT 'UNKNOWN',
    UNIQUE (boot_id, sequence));
CREATE INDEX IF NOT EXISTS events_time ON events (utc_epoch);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, utc_epoch);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    utc TEXT NOT NULL, utc_epoch REAL NOT NULL, clock_sync TEXT NOT NULL,
    monotonic_s REAL NOT NULL, data BLOB NOT NULL,
    source TEXT NOT NULL DEFAULT 'UNKNOWN', clock_source TEXT NOT NULL DEFAULT 'UNKNOWN',
    UNIQUE (boot_id, sequence));
CREATE INDEX IF NOT EXISTS samples_time ON samples (utc_epoch);
CREATE TABLE IF NOT EXISTS boots (
    id INTEGER PRIMARY KEY,
    boot_id TEXT NOT NULL UNIQUE,
    started_utc TEXT, config_hash TEXT, file_hashes TEXT,
    last_utc TEXT, stopped_utc TEXT, stop_reason TEXT);
"""

DAY_S = 86400.0
MIN_PRUNE_ROWS = 50


def _finite(value):
    """Replace NaN/inf (not valid JSON) by None, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def encode(data) -> str:
    try:
        return json.dumps(data, allow_nan=False, default=str, separators=(",", ":"))
    except ValueError:
        return json.dumps(_finite(data), default=str, separators=(",", ":"))


def read_only_uri(path) -> str:
    """SQLite URI for read-only access (forward slashes, also on Windows)."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def decode_sample(blob) -> dict:
    return json.loads(zlib.decompress(blob))


class SqliteStore:
    def __init__(self, path, settings):
        self.path = Path(path)
        self.event_retention_s = settings.event_retention_days * DAY_S
        self.sample_retention_s = settings.sample_retention_days * DAY_S
        self.max_bytes = settings.max_database_mb * 1024 * 1024
        self._db = None
        self._checked = False       # quick_check done for this file
        self._notices = []          # (kind, data) for the event log

    # Connection ------------------------------------------------------------
    def _connect(self):
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=5.0)
            try:
                if not self._checked:
                    result = db.execute("PRAGMA quick_check").fetchone()[0]
                    if result != "ok":
                        raise sqlite3.DatabaseError(
                            f"database disk image is malformed (quick_check: {result})")
                    self._checked = True
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.executescript(SCHEMA)
                self._migrate(db)
                db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                           (str(SCHEMA_VERSION),))
                db.commit()
            except Exception:
                db.close()
                raise
            self._db = db
        return self._db

    @staticmethod
    def _migrate(db):
        for table in ("events", "samples"):
            present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for column in _SOURCE_COLUMNS:
                if column not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} "
                               "TEXT NOT NULL DEFAULT 'UNKNOWN'")

    def reset(self):
        """Drop the connection after an error; the next write reconnects."""
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    close = reset

    # Corruption ------------------------------------------------------------
    def _guarded(self, action, *args):
        try:
            return action(*args)
        except sqlite3.DatabaseError as error:
            if not is_corruption(error):
                raise                       # retried later by the writer
            self._recover(error)
            return action(*args)            # on the fresh database

    def _recover(self, error):
        self.reset()
        preserved = preserve(self.path, time.time())
        self._checked = False
        self._notices.append(("HISTORIAN_DB_RECOVERED", {
            "error": f"{type(error).__name__}: {error}",
            "preserved_as": preserved.name,
            "note": "Corrupt database preserved unchanged; a new database was started",
            **storage_report(self.path),
        }))

    def take_notices(self) -> list:
        notices, self._notices = self._notices, []
        return notices

    def storage(self) -> dict:
        return storage_report(self.path)

    # Writing ---------------------------------------------------------------
    def write(self, records):
        self._guarded(self._write, records)

    def _write(self, records):
        db = self._connect()
        events, samples, boots = [], [], {}
        for r in records:
            base = (r.get("source", "UNKNOWN"), r.get("clock_source", "UNKNOWN"),
                    r["boot_id"], r["sequence"], r["utc"], r["utc_epoch"],
                    r["clock_sync"], r["monotonic_s"])
            if r["table"] == "events":
                events.append(base + (r["kind"], encode(r["data"])))
            else:
                samples.append(base + (zlib.compress(encode(r["data"]).encode()),))
            boots.setdefault(r["boot_id"], []).append(r)
        with db:
            db.executemany(f"INSERT OR IGNORE INTO events ({_COLUMNS}, kind, data) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", events)
            db.executemany(f"INSERT OR IGNORE INTO samples ({_COLUMNS}, data) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", samples)
            for boot_id, rows in boots.items():
                self._update_boot(db, boot_id, rows)

    @staticmethod
    def _update_boot(db, boot_id, rows):
        db.execute("INSERT OR IGNORE INTO boots (boot_id) VALUES (?)", (boot_id,))
        for r in rows:
            if r["table"] != "events":
                continue
            if r["kind"] == "CONTROLLER_START":
                db.execute("UPDATE boots SET started_utc = ?, config_hash = ?, "
                           "file_hashes = ? WHERE boot_id = ?",
                           (r["utc"], r["data"].get("config_hash"),
                            encode(r["data"].get("file_hashes")), boot_id))
            elif r["kind"] == "CONTROLLER_STOP":
                db.execute("UPDATE boots SET stopped_utc = ?, stop_reason = ? "
                           "WHERE boot_id = ?", (r["utc"], r["data"].get("reason"), boot_id))
        db.execute("UPDATE boots SET last_utc = ? WHERE boot_id = ?",
                   (rows[-1]["utc"], boot_id))

    # Reading (startup only, own short connection) ---------------------------
    def previous_boot(self, current_boot_id) -> dict | None:
        if not self.path.exists():
            return None
        db = sqlite3.connect(read_only_uri(self.path), uri=True, timeout=5.0)
        try:
            row = db.execute(
                "SELECT boot_id, started_utc, config_hash, file_hashes, last_utc, "
                "stopped_utc, stop_reason FROM boots WHERE boot_id != ? "
                "ORDER BY id DESC LIMIT 1", (current_boot_id,)).fetchone()
        except sqlite3.OperationalError:
            return None          # empty or older file without the table
        finally:
            db.close()
        if row is None:
            return None
        keys = ("boot_id", "started_utc", "config_hash", "file_hashes", "last_utc",
                "stopped_utc", "stop_reason")
        boot = dict(zip(keys, row))
        boot["file_hashes"] = json.loads(boot["file_hashes"] or "null")
        return boot

    def unacknowledged_recoveries(self) -> list:
        """HISTORIAN_DB_RECOVERED events after the last acknowledgement."""
        if not self.path.exists():
            return []
        db = sqlite3.connect(read_only_uri(self.path), uri=True, timeout=5.0)
        try:
            last_ack = db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events "
                "WHERE kind = 'HISTORIAN_RECOVERY_ACKNOWLEDGED'").fetchone()[0]
            rows = db.execute(
                "SELECT utc, boot_id, sequence, data FROM events "
                "WHERE kind = 'HISTORIAN_DB_RECOVERED' AND id > ? ORDER BY id",
                (last_ack,)).fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            db.close()
        return [{"utc": utc, "boot_id": boot, "sequence": seq, "data": json.loads(data)}
                for utc, boot, seq, data in rows]

    # Retention ---------------------------------------------------------------
    def used_bytes(self) -> int:
        db = self._connect()
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        size = db.execute("PRAGMA page_size").fetchone()[0]
        return (pages - free) * size

    def prune(self, now_epoch: float, *, clock_trusted: bool) -> dict:
        """Age limits (only with a trusted clock), then the size limit.

        Returns the deleted counts and the disk usage, corrupt files separately.
        """
        result = self._guarded(self._prune, now_epoch, clock_trusted)
        return {**result, **storage_report(self.path)}

    def _prune(self, now_epoch, clock_trusted):
        db = self._connect()
        result = {"expired_events": 0, "expired_samples": 0,
                  "size_events": 0, "size_samples": 0}
        if clock_trusted:
            with db:
                result["expired_events"] = db.execute(
                    "DELETE FROM events WHERE utc_epoch < ?",
                    (now_epoch - self.event_retention_s,)).rowcount
                result["expired_samples"] = db.execute(
                    "DELETE FROM samples WHERE utc_epoch < ?",
                    (now_epoch - self.sample_retention_s,)).rowcount
        # Over the size limit: oldest samples first, events only if no samples remain.
        while (used := self.used_bytes()) > self.max_bytes:
            table = "samples" if db.execute("SELECT 1 FROM samples LIMIT 1").fetchone() else "events"
            rows = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            share = (used - self.max_bytes) / used          # about this fraction must go
            count = max(MIN_PRUNE_ROWS, math.ceil(rows * share * 1.05))
            with db:
                removed = db.execute(
                    f"DELETE FROM {table} WHERE id IN "
                    f"(SELECT id FROM {table} ORDER BY id LIMIT ?)", (count,)).rowcount
            if removed == 0:
                break
            result[f"size_{table}"] += removed
        result["used_mb"] = round(self.used_bytes() / 1048576, 3)
        return result
