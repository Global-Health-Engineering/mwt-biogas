"""CSV export of the historian database (run beside the controller, read-only).

Every row starts with `source` (LIVE plant I/O or SIMULATED; UNKNOWN for rows
written before version 2) and `clock_source` (SYSTEM clock or SIMULATED
time), so exported data cannot be mistaken for field data.

events:  one row per event; `data` stays one JSON column (kinds differ).
samples: one row per sample; nested values flattened to dotted columns,
         e.g. temperatures.PT3_B4.celsius.
"""

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from pasteurizer.historian.sqlite_store import decode_sample, read_only_uri

BASE = ("source", "clock_source", "utc", "clock_sync", "boot_id", "sequence", "monotonic_s")


def _epoch(text):
    if text is None:
        return None
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def flatten(value, prefix="") -> dict:
    if isinstance(value, dict):
        flat = {}
        for key, item in value.items():
            flat.update(flatten(item, f"{prefix}{key}."))
        return flat
    if isinstance(value, list):
        return {prefix[:-1]: json.dumps(value)}
    return {prefix[:-1]: value}


def _rows(db, table, since, until, kinds):
    columns = BASE + (("kind",) if table == "events" else ()) + ("data",)
    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    # A database written before version 2 has no source columns: UNKNOWN.
    select = [c if c in present else f"'UNKNOWN' AS {c}" for c in columns]
    query = f"SELECT {', '.join(select)} FROM {table} WHERE 1=1"
    args = []
    if since is not None:
        query += " AND utc_epoch >= ?"
        args.append(since)
    if until is not None:
        query += " AND utc_epoch < ?"
        args.append(until)
    if kinds and table == "events":
        query += f" AND kind IN ({', '.join('?' * len(kinds))})"
        args.extend(kinds)
    query += " ORDER BY id"
    for row in db.execute(query, args):
        yield dict(zip(columns, row))


def export_csv(database, table, output, *, since=None, until=None, kinds=None) -> int:
    """Write one table to CSV; since/until are ISO times (UTC if no zone)."""
    if table not in ("events", "samples"):
        raise ValueError("table must be 'events' or 'samples'")
    since, until = _epoch(since), _epoch(until)
    db = sqlite3.connect(read_only_uri(database), uri=True)
    try:
        if table == "events":
            header = list(BASE) + ["kind", "data"]
        else:   # first pass: the union of all flattened sample columns
            keys = {}
            for row in _rows(db, table, since, until, kinds):
                keys.update(dict.fromkeys(flatten(decode_sample(row["data"]))))
            header = list(BASE) + list(keys)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(output, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=header, extrasaction="ignore")
            writer.writeheader()
            for row in _rows(db, table, since, until, kinds):
                if table == "samples":
                    row.update(flatten(decode_sample(row.pop("data"))))
                writer.writerow(row)
                count += 1
        return count
    finally:
        db.close()
