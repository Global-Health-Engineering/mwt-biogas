"""History for the GUI: events with display fields, trends, filtered CSV export.

All reads go through pasteurizer.historian.queries (read-only, bounded).
Troubleshooting procedures are not documented yet, so alarm rows carry
"No documented procedure" rather than invented instructions.
"""

import csv
import io
import json

from gui.tags import BY_ID
from pasteurizer.alarm_catalogue import info as alarm_info
from pasteurizer.fault_manager import FAULTS
from pasteurizer.historian.queries import HistoryReader, MAX_PAGE

LEVEL = {f.code: f.fault_level for f in FAULTS}
EXPORT_MAX_ROWS = 100_000
NO_PROCEDURE = "No documented procedure"


def _value(v):
    return "ON" if v is True else "OFF" if v is False else v


def _changes(data):
    """`name=VALUE`, with the reason and the reason before when recorded."""
    changes = data.get("changes") or {}
    reasons = data.get("reasons") or {}
    parts = []
    for k, v in changes.items():
        text = f"{k}={_value(v)}"
        why = reasons.get(k)
        if why:
            detail = why.get("reason", "")
            if why.get("blocked_by"):
                detail += f" [blocked: {', '.join(why['blocked_by'])}]"
            if why.get("not_permitted"):
                detail += f" [not permitted: {why['not_permitted']}]"
            before = why.get("previous")
            if before and before.get("reason") != why.get("reason"):
                detail += f"; before: {before.get('reason')}"
            text += f" ({detail})"
        parts.append(text)
    return ", ".join(parts)


# Sensor names in signal events -> equipment id in gui.tags
SENSOR_EQUIPMENT = {"y4_feedback": "control_valve_percent"}


def _sensor(name) -> str:
    """Drawing tag and readable name, e.g. "EDB-LS-04 Tank 1 high"."""
    entry = BY_ID.get(SENSOR_EQUIPMENT.get(name, name))
    if entry is None:
        return name
    if name == "y4_feedback":
        return f"{entry['tag']} position feedback"
    return f"{entry['tag']} {entry['name']}"


def _level(value) -> str:
    return "reached" if value is True else "not reached" if value is False else "unknown"


def _number(value, unit, digits=2):
    return "?" if value is None else f"{value:.{digits}f} {unit}"


def _signal(kind, data) -> str:
    label = _sensor(data.get("sensor"))
    if kind == "LEVEL_CHANGED":
        if data.get("initial"):
            text = f"{label}: {_level(data.get('new'))} (first reading of this run)"
        else:
            text = f"{label}: {_level(data.get('old'))} → {_level(data.get('new'))}"
        if data.get("overridden"):
            text += f"; override active, control uses {_level(data.get('effective'))}"
        return text
    if kind == "SENSOR_VALIDITY":
        if data.get("valid"):
            return f"{label}: signal valid again"
        when = " at start" if data.get("initial") else ""
        return (f"{label}: signal invalid{when} for {data.get('held_s', 0):g} s: "
                f"{data.get('error') or 'no reason given'}")
    if kind == "FLOW_STARTED":
        rate = (_number(data.get("l_min"), "L/min") if data.get("l_min") is not None
                else _number(data.get("pulse_hz"), "Hz (no K-factor)", 1))
        return f"{label}: registering flow, {rate}"
    if kind == "FLOW_STOPPED":
        amount = (_number(data.get("litres"), "L") if data.get("litres") is not None
                  else f"{data.get('pulses')} pulses" if data.get("pulses") is not None
                  else "volume unknown")
        text = (f"{label}: flow stopped after {data.get('duration_s', 0):.0f} s, {amount}, "
                f"average {_number(data.get('avg_l_min'), 'L/min')}, "
                f"peak {_number(data.get('peak_l_min'), 'L/min')} ({data.get('cause')})")
        if data.get("complete") is False:
            text += "; measurement interrupted during the run"
        return text
    return kind


SIGNAL_KINDS = ("LEVEL_CHANGED", "SENSOR_VALIDITY", "FLOW_STARTED", "FLOW_STOPPED")


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
        target = " ".join(f"{key}={data[key]}" for key in ("command", "value", "switch", "code", "cause")
                          if data.get(key) is not None)
        text = (f"{data.get('action')}{' ' + target if target else ''} by {data.get('user')} "
                f"({data.get('role')}): {verdict}, {data.get('outcome')}")
        return "INFO", text, [data[k] for k in ("command", "switch") if data.get(k)], None
    elif kind in ("OUTPUT_WRITTEN", "OUTPUT_REQUESTED", "OUTPUT_NOT_WRITTEN"):
        prefix = {"OUTPUT_WRITTEN": "Written", "OUTPUT_REQUESTED": "Requested",
                  "OUTPUT_NOT_WRITTEN": "NOT WRITTEN (outputs disabled, shadow run); command"}[kind]
        cause = " (shutdown)" if data.get("cause") == "SHUTDOWN" else ""
        text = f"{prefix}{cause}: {_changes(data) or 'no change'}"
        same = data.get("same_cycle_signals") or []
        if same:
            text += "; same scan: " + ", ".join(
                dict.fromkeys(_sensor(s.get("sensor")) for s in same))
        return "INFO", text, list((data.get("changes") or {}).keys()), None
    elif kind in SIGNAL_KINDS:
        sensor = data.get("sensor")
        return ("INFO", _signal(kind, data),
                [SENSOR_EQUIPMENT.get(sensor, sensor)] if sensor else [], None)
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
        elif data.get("output_policy") == "MANUAL_ONLY":       # records of the commissioning gate
            text = (f"Controller started: {data.get('data_source', '?')} inputs, outputs ENABLED "
                    "in MANUAL only (commissioning)")
        elif data.get("data_source") == "LIVE":
            text = "Controller started: LIVE inputs, outputs ENABLED"
        if data.get("watchdog_restart"):
            text += "; after a watchdog shutdown or abnormal stop (WATCHDOG_RESTART latched)"
    elif kind == "INDICATORS":
        lights = [name for name, on in (("warning", data.get("warning")), ("trip", data.get("trip")),
                                        ("release permitted", data.get("release_permitted")),
                                        ("fallback path", data.get("fallback_path"))) if on]
        text = f"Indicator lights for {data.get('process_state')}: {', '.join(lights) or 'none active'}"
    elif kind == "MANUAL_RELEASE_AUTOMATIC":
        text = f"Manual request {data.get('command')} of {data.get('user')} withdrawn: {data.get('cause')}"
    elif kind == "OUTPUT_GATE":            # records of the former commissioning gate
        if data.get("open"):
            text = f"Outputs enabled: selector {data.get('mode')} (policy {data.get('policy')})"
        else:
            text = (f"Outputs held off: selector {data.get('mode')}, policy {data.get('policy')} "
                    "(commissioning: outputs only in MANUAL)")
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
    exclude = [k for k in params.get("exclude_kinds", "").split(",") if k] or None
    return dict(since=params.get("since") or None, until=params.get("until") or None,
                kinds=kinds, exclude_kinds=exclude, severity=params.get("severity") or None,
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
