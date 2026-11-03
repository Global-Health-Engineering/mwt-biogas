"""History for the GUI: events with display fields, trends, filtered CSV export.

All reads go through pasteurizer.historian.queries (read-only, bounded).
Troubleshooting procedures are not documented yet, so alarm rows carry
"No documented procedure" rather than invented instructions.
"""

import csv
import io
import json

from pasteurizer.alarm_catalogue import info as alarm_info
from pasteurizer.fault_manager import FAULTS
from pasteurizer.historian.queries import HistoryReader, MAX_PAGE

LEVEL = {f.code: f.fault_level for f in FAULTS}
EXPORT_MAX_ROWS = 100_000
NO_PROCEDURE = "No documented procedure"


def _changes(data):
    changes = data.get("changes") or {}
    return ", ".join(f"{k}={'ON' if v is True else 'OFF' if v is False else v}"
                     for k, v in changes.items())


def describe(kind, data) -> tuple[str, str, list, str | None]:
    """(severity, description, equipment ids, procedure) for one event."""
    if kind.startswith("ALARM_"):
        code = data.get("code", "")
        level = LEVEL.get(code, 1)
        details = alarm_info(code)
        what = kind[len("ALARM_"):].lower()
        return ("CRITICAL" if level >= 3 else "WARNING",
                f"{code} {what}: {data.get('message', '')}",
                list(details.equipment), details.procedure or NO_PROCEDURE)
    if kind == "STATE_TRANSITION":
        text = f"{data.get('old')} → {data.get('new')}: {data.get('reason')}"
    elif kind == "MODE_CHANGE":
        text = f"Selector {data.get('old')} → {data.get('new')}"
    elif kind == "OPERATOR_ACTION":
        verdict = "accepted" if data.get("accepted") else "refused"
        text = (f"{data.get('action')} by {data.get('user')} ({data.get('role')}): "
                f"{verdict}, {data.get('outcome')}")
    elif kind in ("OUTPUT_WRITTEN", "OUTPUT_REQUESTED", "OUTPUT_NOT_WRITTEN"):
        prefix = {"OUTPUT_WRITTEN": "Written", "OUTPUT_REQUESTED": "Requested",
                  "OUTPUT_NOT_WRITTEN": "NOT WRITTEN (outputs disabled, shadow run); command"}[kind]
        cause = " (shutdown)" if data.get("cause") == "SHUTDOWN" else ""
        text = f"{prefix}{cause}: {_changes(data) or 'no change'}"
        return "INFO", text, list((data.get("changes") or {}).keys()), None
    elif kind == "OUTPUT_WRITE_FAILED":
        text = data.get("note", "Output frame not confirmed as written")
    elif kind == "LOG_GAP":
        text = (f"Historian gap ({data.get('cause')}): {data.get('dropped_events')} events, "
                f"{data.get('dropped_samples')} samples dropped")
    elif kind == "MODEL_STATUS":
        text = f"Model {data.get('status')}, usable {data.get('usable')}: {data.get('reason')}"
    elif kind == "CONTROLLER_START":
        text = f"Controller started ({data.get('data_source', '?')} data)"
        if data.get("outputs_enabled") is False:
            text = (f"Controller started: {data.get('data_source', '?')} inputs, outputs DISABLED "
                    "(shadow run, nothing is written)")
    elif kind == "INDICATORS":
        lights = [name for name, on in (("warning", data.get("warning")), ("trip", data.get("trip")),
                                        ("release permitted", data.get("release_permitted")),
                                        ("fallback path", data.get("fallback_path"))) if on]
        text = f"Indicator lights for {data.get('process_state')}: {', '.join(lights) or 'none active'}"
    elif kind == "CONTROLLER_STOP":
        text = f"Controller stopped: {data.get('reason')}"
        if data.get("outputs_enabled") is False:
            text += "; safe frame NOT written (outputs disabled)"
    else:
        text = f"{kind}: {json.dumps(data, separators=(',', ':'))[:200]}"
    return "INFO", text, [], None


def _decorate(row):
    severity, text, equipment, procedure = describe(row["kind"], row["data"])
    row.update(severity=severity, description=text, equipment=equipment, procedure=procedure)
    return row


def _filters(params):
    kinds = [k for k in params.get("kinds", "").split(",") if k] or None
    return dict(since=params.get("since") or None, until=params.get("until") or None,
                kinds=kinds, severity=params.get("severity") or None,
                equipment=params.get("equipment") or None)


def events(database, params) -> dict:
    with HistoryReader(database) as history:
        page = history.events(
            **_filters(params),
            before_id=params.get("before_id") or None,
            after_id=params.get("after_id") or None,
            limit=min(int(params.get("limit", 100)), MAX_PAGE))
    return {"rows": [_decorate(r) for r in page.rows],
            "next_before_id": page.next_before_id, "newest_id": page.newest_id}


def trend(database, params) -> dict:
    names = [n for n in params.get("series", "").split(",") if n]
    with HistoryReader(database) as history:
        return history.trend(names, since=params["since"], until=params["until"],
                             max_points=int(params.get("max_points", 1000)))


def runs(database) -> list:
    with HistoryReader(database) as history:
        return history.runs()


EXPORT_COLUMNS = ("source", "clock_source", "utc", "clock_sync", "boot_id", "sequence",
                  "severity", "kind", "description", "equipment", "procedure", "data")


def export_events(database, params):
    """CSV text chunks of the filtered events, newest first, at most 100 000 rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    yield buffer.getvalue()
    before, total = None, 0
    with HistoryReader(database) as history:
        while total < EXPORT_MAX_ROWS:
            page = history.events(**_filters(params), before_id=before, limit=MAX_PAGE)
            buffer.seek(0)
            buffer.truncate()
            for row in page.rows:
                row = _decorate(row)
                writer.writerow([row["source"], row["clock_source"], row["utc"],
                                 row["clock_sync"], row["boot_id"], row["sequence"],
                                 row["severity"], row["kind"], row["description"],
                                 " ".join(row["equipment"]), row["procedure"] or "",
                                 json.dumps(row["data"], separators=(",", ":"))])
            total += len(page.rows)
            yield buffer.getvalue()
            if page.next_before_id is None:
                break
            before = page.next_before_id
