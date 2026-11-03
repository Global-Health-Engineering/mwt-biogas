"""Read-only, bounded history queries for the GUI (never used by the scan).

Each call opens the database read-only, runs indexed queries with a row cap
and closes it again, so a GUI request can never write, lock out the writer
for long, or pull an unbounded result into memory.

events()  newest first, paged by id (`before_id` scrolls back, `after_id`
          follows live); filters: time range, kinds, severity, equipment
trend()   one series per name, decimated to about `max_points`; every point
          carries its run (boot_id) so lines break between controller runs,
          invalid readings are None, and logged gaps are returned separately
runs()    controller runs with start, stop and config hash
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from pasteurizer.alarm_catalogue import info as alarm_info
from pasteurizer.fault_manager import FAULTS
from pasteurizer.historian.sqlite_store import decode_sample, read_only_uri

MAX_PAGE = 500
MAX_TREND_POINTS = 5000
SOURCE_COLUMNS = ("source", "clock_source")

# name -> (path in a sample, validity path or None, units)
SERIES = {
    **{f"PT{n}": (("temperatures", tag, "celsius"), ("temperatures", tag, "valid"), "degC")
       for n, tag in enumerate(("PT1_B2", "PT2_B3", "PT3_B4", "PT4_B5", "PT5_B6", "PT6_B7"),
                               start=1)},
    **{f"FLOW{n}": (("flows", f"flow_sensor_{n}", "l_min"),
                    ("flows", f"flow_sensor_{n}", "valid"), "L/min") for n in (1, 2, 3)},
    "Y4_REQUESTED": (("requested", "control_valve_percent"), None, "%"),
    "Y4_WRITTEN": (("written", "control_valve_percent"), None, "%"),
    "Y4_FEEDBACK": (("y4_feedback", "percent"), ("y4_feedback", "valid"), "%"),
}


@dataclass(frozen=True)
class Page:
    rows: list
    next_before_id: int | None      # pass as before_id for older rows
    newest_id: int | None           # pass as after_id to follow live


def _epoch(text):
    if text is None:
        return None
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _pick(sample, path):
    value = sample
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _codes(severity=None, equipment=None) -> list:
    codes = []
    for fault in FAULTS:
        level = "CRITICAL" if fault.fault_level >= 3 else "WARNING"
        if severity and level != severity:
            continue
        if equipment and equipment not in alarm_info(fault.code).equipment:
            continue
        codes.append(fault.code)
    return codes


class HistoryReader:
    """Use as `with HistoryReader(path) as history: history.events(...)`."""

    def __init__(self, database):
        self.path = Path(database)
        self._db = None

    def _connect(self):
        db = sqlite3.connect(read_only_uri(self.path), uri=True, timeout=2.0)
        db.execute("PRAGMA query_only = ON")
        return db

    def _select_columns(self, db, table, columns):
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        return [c if c in present else f"'UNKNOWN' AS {c}" for c in columns]

    # Events ----------------------------------------------------------------
    def events(self, *, since=None, until=None, kinds=None, severity=None,
               equipment=None, before_id=None, after_id=None, limit=100) -> Page:
        limit = max(1, min(int(limit), MAX_PAGE))
        columns = ("id",) + SOURCE_COLUMNS + ("utc", "utc_epoch", "clock_sync", "boot_id",
                                              "sequence", "monotonic_s", "kind", "data")
        where, args = [], []
        if since is not None:
            where.append("utc_epoch >= ?")
            args.append(_epoch(since))
        if until is not None:
            where.append("utc_epoch < ?")
            args.append(_epoch(until))
        if kinds:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args.extend(kinds)
        if severity or equipment:
            codes = _codes(severity, equipment)
            alarm = (f"(kind LIKE 'ALARM_%' AND json_extract(data, '$.code') IN "
                     f"({', '.join('?' * len(codes)) or 'NULL'}))")
            args.extend(codes)
            if equipment and not severity:
                # Output events that changed this command count as its events too.
                alarm = f"({alarm} OR json_extract(data, ?) IS NOT NULL)"
                args.append(f"$.changes.{equipment}")
            where.append(alarm)
        if before_id is not None:
            where.append("id < ?")
            args.append(int(before_id))
        order = "DESC"
        if after_id is not None:
            where.append("id > ?")
            args.append(int(after_id))
            order = "ASC"
        query = (f"SELECT {', '.join(self._select_columns(self._db, 'events', columns))} "
                 f"FROM events {'WHERE ' + ' AND '.join(where) if where else ''} "
                 f"ORDER BY id {order} LIMIT ?")
        args.append(limit)
        rows = [dict(zip(columns, r)) for r in self._db.execute(query, args)]
        for row in rows:
            row["data"] = json.loads(row["data"])
        if order == "ASC":
            rows.reverse()                      # always newest first
        newest = self._db.execute("SELECT MAX(id) FROM events").fetchone()[0]
        older = rows[-1]["id"] if len(rows) == limit else None
        return Page(rows, older, newest)

    # Trends ------------------------------------------------------------------
    def trend(self, names, *, since, until, max_points=1000) -> dict:
        unknown = [n for n in names if n not in SERIES]
        if unknown:
            raise ValueError(f"Unknown series: {unknown}")
        max_points = max(10, min(int(max_points), MAX_TREND_POINTS))
        lo, hi = _epoch(since), _epoch(until)
        db = self._db
        count = db.execute("SELECT COUNT(*) FROM samples WHERE utc_epoch >= ? AND utc_epoch < ?",
                           (lo, hi)).fetchone()[0]
        stride = max(1, math.ceil(count / max_points))
        cols = ", ".join(self._select_columns(db, "samples", SOURCE_COLUMNS))
        rows = db.execute(
            f"SELECT id, {cols}, utc, utc_epoch, boot_id, data FROM samples "
            "WHERE utc_epoch >= ? AND utc_epoch < ? AND (id % ?) = 0 ORDER BY id",
            (lo, hi, stride)).fetchall()
        series = {name: {"units": SERIES[name][2], "points": []} for name in names}
        sources = set()
        for _, source, clock, utc, epoch, boot, blob in rows:
            sources.add((source, clock))
            sample = decode_sample(blob)
            for name in names:
                path, valid_path, _ = SERIES[name]
                value = _pick(sample, path)
                if valid_path is not None and _pick(sample, valid_path) is not True:
                    value = None
                series[name]["points"].append({"utc": utc, "t": epoch, "run": boot,
                                               "value": value})
        gaps = [{"utc": e["utc"], "data": e["data"]} for e in self.events(
            since=since, until=until, kinds=["LOG_GAP"], limit=MAX_PAGE).rows]
        return {"series": series, "stride": stride, "samples_in_range": count,
                "sources": sorted(sources), "gaps": gaps}

    # Runs -------------------------------------------------------------------
    def runs(self, limit=50) -> list:
        rows = self._db.execute(
            "SELECT boot_id, started_utc, last_utc, stopped_utc, stop_reason, config_hash "
            "FROM boots ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), MAX_PAGE)),))
        keys = ("boot_id", "started_utc", "last_utc", "stopped_utc", "stop_reason",
                "config_hash")
        return [dict(zip(keys, r)) for r in rows]

    # One connection per call --------------------------------------------------
    def __enter__(self):
        self._db = self._connect()
        return self

    def __exit__(self, *exc):
        self._db.close()
        self._db = None
