"""Analyse Y4 command steps in an exported historian samples CSV (read-only).

Reads a CSV written by `tools/export_history.py samples ...`, finds every held
Y4 command (plateau), and reports per plateau the commanded and measured
opening, final-window flow of flow sensor 3, spread and quality flags, and per
step the direction, flow gain and approximate response timing. No hardware
access, no database access, nothing is written except an optional --out CSV.

    python3 tools/analyze_y4_steps.py exports/y4_steps.csv
    python3 tools/analyze_y4_steps.py exports/y4_steps.csv --final-window 30 --min-hold 60
    python3 tools/analyze_y4_steps.py exports/y4_steps.csv --target 0.3 0.5 --out exports/y4_plateaus.csv
    python3 tools/analyze_y4_steps.py other.csv --cmd-col requested.control_valve_percent

Default columns (historian sample export):
  time     monotonic_s                      (seconds; split at boot_id changes)
  label    utc                              (shown in the table)
  command  written.control_valve_percent    (empty = frame not confirmed written)
  measured y4_feedback.percent  (+ y4_feedback.valid)
  flow     flows.flow_sensor_3.l_min  (+ .valid, .calibrated)
Only rows with mode == MANUAL are analysed unless --any-mode is given.

Timing is limited by the sample interval (logging.periodic_interval_s, 5 s by
default). The step instant is the midpoint between the last sample of the old
command and the first of the new one (+/- half an interval). delay10 and t63
(10 % and 63 % of the flow change) are shown as "a-b": the crossing happened
between those two samples. settle is the first sample after which the flow
stays inside the band; "<N" means it was already settled at the first sample.
"""

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = ROOT / "config" / "control_settings.json"

DEFAULT_COLUMNS = {
    "time": "monotonic_s",
    "label": "utc",
    "cmd": "written.control_valve_percent",
    "req": "requested.control_valve_percent",
    "pos": "y4_feedback.percent",
    "pos_valid": "y4_feedback.valid",
    "flow": "flows.flow_sensor_3.l_min",
    "flow_valid": "flows.flow_sensor_3.valid",
    "flow_cal": "flows.flow_sensor_3.calibrated",
    "mode": "mode",
    "boot": "boot_id",
    "source": "source",
}
REQUIRED = ("time", "cmd", "pos", "flow")
HINTS = {"time": ("monotonic", "utc", "time"), "cmd": ("control_valve", "y4"),
         "pos": ("y4", "feedback", "position"), "flow": ("flow",)}


class AnalysisError(Exception):
    pass


# Parsing ---------------------------------------------------------------------
def _num(text):
    if text is None:
        return None
    text = text.strip()
    if text in ("", "None", "null", "nan", "NaN"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _bool(text):
    if text is None:
        return None
    text = text.strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def _time(text):
    value = _num(text)
    if value is not None:
        return value
    if not text or not text.strip():
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


@dataclass
class Sample:
    row: int
    t: float
    label: str
    cmd: float | None
    req: float | None
    pos: float | None
    flow: float | None
    flow_cal: bool | None
    mode: str | None
    boot: str | None
    source: str | None


def resolve_columns(header, args):
    cols = dict(DEFAULT_COLUMNS)
    for key in cols:
        override = getattr(args, f"{key}_col", None)
        if override is not None:
            cols[key] = override or None            # "" disables an optional column
    notes = []
    # Time: fall back to utc when the export has no monotonic column.
    if args.time_col is None and cols["time"] not in header and "utc" in header:
        cols["time"] = "utc"
        notes.append("no monotonic_s column: using utc for timing (clock jumps not detected)")
    # Command: fall back to the requested value only when no written column exists.
    if args.cmd_col is None and cols["cmd"] not in header and cols["req"] in header:
        notes.append(f"no {cols['cmd']} column: using {cols['req']} (BEFORE the arbiter; "
                     "blocked commands are not visible)")
        cols["cmd"] = cols["req"]
    missing = [k for k in REQUIRED if not cols[k] or cols[k] not in header]
    if missing:
        lines = []
        for key in missing:
            near = [h for h in header if any(w in h.lower() for w in HINTS[key])][:8]
            lines.append(f"  {key}: column '{cols[key]}' not found"
                         + (f"; candidates: {', '.join(near)}" if near else "")
                         + f"  (override with --{key}-col)")
        raise AnalysisError("required signal missing from CSV:\n" + "\n".join(lines))
    for key in cols:
        if key not in REQUIRED and cols[key] and cols[key] not in header:
            if getattr(args, f"{key}_col", None):
                raise AnalysisError(f"column '{cols[key]}' (--{key.replace('_', '-')}-col) not in CSV")
            cols[key] = None
    if not cols["flow_valid"]:
        notes.append("no flow validity column: every numeric flow value is used")
    if not cols["pos_valid"]:
        notes.append("no Y4 feedback validity column: every numeric position is used")
    return cols, notes


def load(path, args):
    try:
        file = open(path, newline="", encoding="utf-8-sig")
    except OSError as error:
        raise AnalysisError(f"cannot open {path}: {error}") from None
    with file:
        reader = csv.DictReader(file)
        header = reader.fieldnames or []
        if not header:
            raise AnalysisError(f"{path} is empty or has no header")
        cols, notes = resolve_columns(header, args)
        samples, bad_time = [], 0
        for number, raw in enumerate(reader, start=2):
            get = lambda key: raw.get(cols[key]) if cols[key] else None   # noqa: E731
            t = _time(get("time"))
            if t is None:
                bad_time += 1
                continue
            pos = _num(get("pos"))
            if cols["pos_valid"] and _bool(get("pos_valid")) is False:
                pos = None
            flow = _num(get("flow"))
            if cols["flow_valid"] and _bool(get("flow_valid")) is False:
                flow = None
            samples.append(Sample(
                row=number, t=t, label=(get("label") or "").strip(),
                cmd=_num(get("cmd")), req=_num(get("req")), pos=pos, flow=flow,
                flow_cal=_bool(get("flow_cal")) if cols["flow_cal"] else None,
                mode=(get("mode") or "").strip() or None if cols["mode"] else None,
                boot=(get("boot") or "").strip() or None if cols["boot"] else None,
                source=(get("source") or "").strip() or None if cols["source"] else None))
    if bad_time:
        notes.append(f"{bad_time} rows without a readable time skipped")
    if not samples:
        raise AnalysisError("no rows with a readable time")
    return samples, cols, notes


# Plateaus --------------------------------------------------------------------
@dataclass
class Plateau:
    index: int
    cmd: float
    samples: list
    prev_t: float | None            # last sample of the previous command (same run)
    prev_plateau: "Plateau | None"
    end_reason: str = "end of data"
    flags: list = field(default_factory=list)
    # filled by characterise()
    t_step: float = 0.0
    hold_s: float = 0.0
    n_window: int = 0
    pos_med: float | None = None
    flow_med: float | None = None
    flow_spread: float | None = None
    flow_peak: float | None = None
    flow_drift: float | None = None
    pos_drift: float | None = None
    max_gap: float | None = None
    usable: bool = False

    @property
    def direction(self):
        prev = self.prev_plateau
        if prev is None:
            return "-"
        return "OPEN" if self.cmd > prev.cmd else "CLOSE"


def segment(samples, args, notes):
    """Split samples into runs of one held command."""
    plateaus, current, last = [], None, None
    dropped_mode = blocked_rows = 0

    def close(reason):
        nonlocal current
        if current is not None:
            current.end_reason = reason
            plateaus.append(current)
        current = None

    for s in samples:
        eligible = s.cmd is not None
        if eligible and not args.any_mode and s.mode is not None and s.mode != "MANUAL":
            eligible = False
            dropped_mode += 1
        contiguous = (last is not None and s.boot == last.boot
                      and 0 < s.t - last.t <= args.max_gap)
        if current is not None and not contiguous:
            close("time gap" if last is not None and s.boot == last.boot else "restart")
        if not eligible:
            if current is not None:
                close("not MANUAL" if s.cmd is not None else "not written")
            last = s
            continue
        if s.req is not None and abs(s.req - s.cmd) > args.cmd_tol:
            blocked_rows += 1
        if current is not None and abs(s.cmd - current.cmd) > args.cmd_tol:
            prev = current
            close("command change")
            current = Plateau(len(plateaus) + 1, s.cmd, [s], last.t, prev)
        elif current is None:
            current = Plateau(len(plateaus) + 1, s.cmd, [s], None, None)
        else:
            current.samples.append(s)
        last = s
    close("end of data")
    if dropped_mode:
        notes.append(f"{dropped_mode} rows outside MANUAL ignored (use --any-mode to include)")
    if blocked_rows:
        notes.append(f"{blocked_rows} rows where requested != written Y4 (arbiter/interlock, e.g. "
                     "Y4 waits for Y3): the WRITTEN value is analysed")
    return plateaus


def _slope(points):
    """Least-squares slope dy/dt; None with fewer than 3 points."""
    if len(points) < 3:
        return None
    ts = [p[0] for p in points]
    ys = [p[1] for p in points]
    mt, my = statistics.fmean(ts), statistics.fmean(ys)
    den = sum((t - mt) ** 2 for t in ts)
    if den <= 0:
        return None
    return sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / den


def characterise(p, args, dt):
    s = p.samples
    first, end = s[0].t, s[-1].t
    p.t_step = (p.prev_t + first) / 2 if p.prev_t is not None else first
    p.hold_s = end - p.t_step
    gaps = [b.t - a.t for a, b in zip(s, s[1:])]
    p.max_gap = max(gaps) if gaps else None
    window = [x for x in s if x.t >= end - args.final_window]
    flows = [(x.t, x.flow) for x in window if x.flow is not None]
    poss = [(x.t, x.pos) for x in window if x.pos is not None]
    all_flows = [x.flow for x in s if x.flow is not None]
    p.n_window = len(flows)
    if flows:
        values = [f for _, f in flows]
        p.flow_med = statistics.median(values)
        p.flow_spread = max(values) - min(values)
    if all_flows:
        p.flow_peak = max(all_flows)
    if poss:
        p.pos_med = statistics.median([v for _, v in poss])
    span = (flows[-1][0] - flows[0][0]) if len(flows) > 1 else 0.0
    slope = _slope(flows)
    p.flow_drift = slope * span if slope is not None else None
    pslope = _slope(poss)
    p.pos_drift = pslope * ((poss[-1][0] - poss[0][0]) if len(poss) > 1 else 0) if pslope else 0.0

    flags = p.flags
    if p.hold_s < args.min_hold:
        flags.append("SHORT")
    if p.n_window < args.min_samples or (p.max_gap is not None and dt and p.max_gap > 2.5 * dt):
        flags.append("SPARSE")
    if p.flow_drift is not None and abs(p.flow_drift) > max(args.drift_tol,
                                                            args.drift_frac * abs(p.flow_med or 0)):
        flags.append("DRIFT")
    if p.pos_drift is not None and abs(p.pos_drift) > args.pos_drift_tol:
        flags.append("POS-DRIFT")
    invalid_flow = sum(1 for x in s if x.flow is None)
    if s and invalid_flow / len(s) > 0.2:
        flags.append("FLOW-INVALID")
    if s and sum(1 for x in s if x.pos is None) / len(s) > 0.2:
        flags.append("POS-INVALID")
    if any(x.flow_cal is False for x in s):
        flags.append("UNCAL")
    if any(x.req is not None and abs(x.req - x.cmd) > args.cmd_tol for x in s):
        flags.append("BLOCKED")
    if p.end_reason != "command change":
        flags.append("OPEN-END" if p.end_reason == "end of data" else f"END:{p.end_reason}")
    p.usable = (p.flow_med is not None
                and not {"SHORT", "SPARSE", "DRIFT", "FLOW-INVALID"} & set(flags))


# Steps -------------------------------------------------------------------------
@dataclass
class Step:
    plateau: Plateau
    prev: Plateau
    d_cmd: float
    d_pos: float | None
    d_flow: float | None
    gain_cmd: float | None = None
    gain_pos: float | None = None
    delay: str = "-"
    t63: str = "-"
    settle: str = "-"
    pos_settle: str = "-"
    grace_flow: float | None = None    # flow at the first sample >= grace after the step
    usable: bool = False
    note: str = ""


def _fmt_t(value, lower_bound):
    if value is None:
        return "-"
    if value <= lower_bound + 1e-9:
        return f"<{lower_bound:.0f}"
    return f"{value:.0f}"


def _crossing(points, t_step, f0, delta, fraction):
    """(earliest, latest) time from the step at which the response passed the
    fraction: between the last sample below and the first sample at or above."""
    before = 0.0
    for t, v in points:
        if (v - f0) / delta >= fraction:
            return max(0.0, before), t - t_step
        before = t - t_step
    return None


def _fmt_range(bounds):
    if bounds is None:
        return "-"
    lo, hi = bounds
    return f"{lo:.0f}-{hi:.0f}" if hi - lo >= 1 else f"{hi:.0f}"


def _settling(points, t_step, final, band):
    """Time of the first sample after which every sample stays inside the band."""
    settled_at = None
    for t, v in points:
        if abs(v - final) <= band:
            if settled_at is None:
                settled_at = t
        else:
            settled_at = None
    return None if settled_at is None else settled_at - t_step


def analyse_step(p, args):
    prev = p.prev_plateau
    st = Step(p, prev, p.cmd - prev.cmd,
              None if p.pos_med is None or prev.pos_med is None else p.pos_med - prev.pos_med,
              None if p.flow_med is None or prev.flow_med is None else p.flow_med - prev.flow_med)
    first_lag = p.samples[0].t - p.t_step          # how late the first new sample is
    flows = [(x.t, x.flow) for x in p.samples if x.flow is not None]
    after = [v for t, v in flows if t - p.t_step >= args.grace]
    st.grace_flow = after[0] if after else None
    reasons = []
    # The previous hold is only the baseline: it must be settled, not long.
    if prev.flow_med is None or {"SPARSE", "DRIFT", "FLOW-INVALID"} & set(prev.flags):
        reasons.append(f"baseline #{prev.index} not settled")
    elif not prev.usable:
        reasons.append(f"baseline #{prev.index} short but flat")
    if not p.usable:
        reasons.append("plateau not usable")
    st.usable = not [r for r in reasons if "short but flat" not in r]
    if st.d_flow is None:
        st.note = "; ".join(reasons + ["no flow"])
        return st
    if st.usable:
        st.gain_cmd = st.d_flow / st.d_cmd if abs(st.d_cmd) > 1e-9 else None
        if st.d_pos is not None and abs(st.d_pos) >= args.min_pos_change:
            st.gain_pos = st.d_flow / st.d_pos
    noise = max(prev.flow_spread or 0.0, p.flow_spread or 0.0)
    if abs(st.d_flow) < max(args.min_response, 2 * noise):
        reasons.append("flow change within noise: no timing")
    elif st.usable:
        d = st.d_flow
        st.delay = _fmt_range(_crossing(flows, p.t_step, prev.flow_med, d, 0.10))
        st.t63 = _fmt_range(_crossing(flows, p.t_step, prev.flow_med, d, 0.63))
        band = max(args.settle_frac * abs(d), args.settle_abs, (p.flow_spread or 0) / 2)
        settle = _settling(flows, p.t_step, p.flow_med, band)
        st.settle = _fmt_t(settle, first_lag)
        if settle is None:
            reasons.append("flow never settled")
    if st.d_pos is not None and abs(st.d_pos) >= args.min_pos_change and st.usable:
        poss = [(x.t, x.pos) for x in p.samples if x.pos is not None]
        band = max(args.settle_frac * abs(st.d_pos), args.pos_settle_abs)
        st.pos_settle = _fmt_t(_settling(poss, p.t_step, p.pos_med, band), first_lag)
    st.note = "; ".join(reasons)
    return st


# Settings (read only, for annotation) -----------------------------------------
def read_settings(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fb, stop, log = data.get("fallback", {}), data.get("treatment_stop", {}), data.get("logging", {})
    start = data.get("start", {})
    return {
        "start_opening_percent": fb.get("start_opening_percent"),
        "max_flow_l_min": fb.get("max_flow_l_min"),
        "min_flow_l_min": stop.get("min_flow_l_min"),
        "startup_grace_s": stop.get("startup_grace_s"),
        "periodic_interval_s": log.get("periodic_interval_s"),
        "y4_closed_tolerance_percent": start.get("y4_closed_tolerance_percent"),
    }


# Report ------------------------------------------------------------------------
def _f(value, fmt="{:.2f}", none="-"):
    if value is None:
        return none
    text = fmt.format(value)
    if text[:1] == "-" and not text.strip("-+0.,"):
        text = text[1:]                     # "-0.0" -> "0.0"
    return text


def table(headers, rows):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    line = "  ".join(str(h).rjust(w) for h, w in zip(headers, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(str(c).rjust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(out)


def in_band(p, lo, hi):
    return p.usable and p.flow_med is not None and lo <= p.flow_med <= hi


def report(path, samples, cols, notes, plateaus, steps, dt, args, settings):
    lo, hi = args.target
    print(f"Y4 step analysis: {path}")
    print(f"  rows {len(samples)}, plateaus {len(plateaus)}, sample interval ~{_f(dt, '{:.1f}')} s "
          f"(timing +/- {_f(dt / 2 if dt else None, '{:.1f}')} s)")
    print(f"  command={cols['cmd']}  measured={cols['pos']}  flow={cols['flow']}  time={cols['time']}")
    sources = sorted({s.source for s in samples if s.source})
    if sources and sources != ["LIVE"]:
        notes.append(f"source column is {'/'.join(sources)}, not LIVE field data")
    print(f"  final window {args.final_window:.0f} s, min hold {args.min_hold:.0f} s, "
          f"min samples {args.min_samples}, target {lo}-{hi} L/min")
    for note in notes:
        print(f"  note: {note}")
    print()

    rows = []
    for p in plateaus:
        mark = "*" if in_band(p, lo, hi) else ""
        rows.append([f"{p.index}{mark}", p.direction, _f(p.cmd, "{:.1f}"), _f(p.pos_med, "{:.1f}"),
                     _f(p.flow_med, "{:.3f}"), _f(p.flow_spread, "{:.3f}"), _f(p.flow_peak, "{:.3f}"),
                     _f(p.flow_drift, "{:+.3f}"), p.n_window, f"{p.hold_s:.0f}",
                     p.samples[0].label[11:19] if len(p.samples[0].label) >= 19 else p.samples[0].label,
                     "yes" if p.usable else "no", ",".join(p.flags)])
    print("Plateaus (* = usable and median flow inside the target band)")
    print(table(["#", "dir", "cmd%", "pos%", "flow", "spread", "peak", "drift", "n", "hold_s",
                 "start", "usable", "flags"], rows))
    print()

    if steps:
        srows = []
        for st in steps:
            p = st.plateau
            srows.append([p.index, p.direction, f"{st.prev.cmd:.1f}->{p.cmd:.1f}",
                          _f(st.d_cmd, "{:+.1f}"), _f(st.d_pos, "{:+.1f}"), _f(st.d_flow, "{:+.3f}"),
                          _f(st.gain_cmd, "{:+.4f}"), _f(st.gain_pos, "{:+.4f}"),
                          st.delay, st.t63, st.settle, st.pos_settle, st.note])
        print("Steps (gain in L/min per percentage point; times in s from the step)")
        print(table(["#", "dir", "cmd", "dcmd", "dpos", "dflow", "dF/dcmd", "dF/dpos",
                     "delay10", "t63", "settle", "pos_set", "note"], srows))
        print()

    good = [p for p in plateaus if in_band(p, lo, hi)]
    print(f"Commands giving {lo}-{hi} L/min (usable plateaus):")
    if good:
        for direction in ("OPEN", "CLOSE", "-"):
            hits = [p for p in good if p.direction == direction]
            if hits:
                label = {"OPEN": "opening", "CLOSE": "closing", "-": "first hold"}[direction]
                items = ", ".join(f"{p.cmd:.1f}% (pos {_f(p.pos_med, '{:.1f}')}%, "
                                  f"{p.flow_med:.3f} L/min, #{p.index})" for p in hits)
                print(f"  {label:10s} {items}")
    else:
        near = sorted((p for p in plateaus if p.usable and p.flow_med is not None),
                      key=lambda p: min(abs(p.flow_med - lo), abs(p.flow_med - hi)))[:3]
        print("  none" + ("; nearest: " + ", ".join(
            f"{p.cmd:.1f}% -> {p.flow_med:.3f} L/min" for p in near) if near else ""))
    print()

    # Hysteresis: same command reached while opening and while closing.
    by_cmd = {}
    for p in plateaus:
        if p.usable and p.direction in ("OPEN", "CLOSE"):
            by_cmd.setdefault(round(p.cmd, 1), {}).setdefault(p.direction, []).append(p)
    pairs = [(c, d) for c, d in sorted(by_cmd.items()) if "OPEN" in d and "CLOSE" in d]
    print("Hysteresis (same command, approached opening vs closing):")
    if pairs:
        hrows = []
        for c, d in pairs:
            o = statistics.median(p.flow_med for p in d["OPEN"])
            k = statistics.median(p.flow_med for p in d["CLOSE"])
            po = [p.pos_med for p in d["OPEN"] if p.pos_med is not None]
            pk = [p.pos_med for p in d["CLOSE"] if p.pos_med is not None]
            hrows.append([f"{c:.1f}", f"{o:.3f}", f"{k:.3f}", f"{k - o:+.3f}",
                          _f(statistics.median(po) if po else None, "{:.1f}"),
                          _f(statistics.median(pk) if pk else None, "{:.1f}")])
        print(table(["cmd%", "flow_open", "flow_close", "close-open", "pos_open", "pos_close"], hrows))
    else:
        print("  no command held in both directions (repeat a command from above and below)")
    print()

    if settings:
        grace, fmin, fmax = settings["startup_grace_s"], settings["min_flow_l_min"], settings["max_flow_l_min"]
        closed = settings["y4_closed_tolerance_percent"] or 0.0
        print(f"Treatment-start view (fallback.start_opening_percent = {settings['start_opening_percent']}; "
              f"after {grace} s flow must be >= {fmin} L/min, never > {fmax} L/min):")
        starts = [st for st in steps if st.plateau.direction == "OPEN" and st.prev.cmd <= closed]
        if starts:
            r = []
            for st in starts:
                p = st.plateau
                ok_grace = (st.grace_flow is not None and fmin is not None and st.grace_flow >= fmin)
                ok_peak = (p.flow_peak is not None and fmax is not None and p.flow_peak <= fmax)
                r.append([p.index, f"{p.cmd:.1f}", _f(st.grace_flow, "{:.3f}"),
                          "yes" if ok_grace else "NO", _f(p.flow_peak, "{:.3f}"),
                          "yes" if ok_peak else "NO", _f(p.flow_med, "{:.3f}"), "yes" if p.usable else "no"])
            print(table(["#", "cmd%", f"flow@>={grace:.0f}s", ">=min", "peak", "<=max", "final", "usable"], r))
            print(f"  flow@ is the first sample at least {grace} s after the step; with "
                  f"~{_f(dt, '{:.0f}')} s sampling it is a coarse check.")
        else:
            print(f"  no opening step from closed (<= {closed}%) found: start each start-candidate from 0%")
        print()


def _r(value, digits=4):
    return "" if value is None else round(value, digits)


def write_out(path, plateaus, steps):
    step_of = {st.plateau.index: st for st in steps}
    with open(path, "w", newline="", encoding="utf-8") as file:
        w = csv.writer(file)
        w.writerow(["plateau", "direction", "start_label", "cmd_percent", "pos_median_percent",
                    "flow_median_l_min", "flow_spread_l_min", "flow_peak_l_min", "flow_drift_l_min",
                    "n_window", "hold_s", "usable", "flags", "prev_cmd_percent", "d_cmd", "d_pos",
                    "d_flow", "gain_per_cmd", "gain_per_pos", "delay10_s", "t63_s", "settle_s",
                    "pos_settle_s", "step_usable", "step_note"])
        for p in plateaus:
            st = step_of.get(p.index)
            w.writerow([p.index, p.direction, p.samples[0].label, p.cmd, _r(p.pos_med, 2),
                        _r(p.flow_med), _r(p.flow_spread), _r(p.flow_peak), _r(p.flow_drift),
                        p.n_window, round(p.hold_s, 1), p.usable, " ".join(p.flags)]
                       + ([st.prev.cmd, _r(st.d_cmd, 2), _r(st.d_pos, 2), _r(st.d_flow),
                           _r(st.gain_cmd, 5), _r(st.gain_pos, 5),
                           st.delay, st.t63, st.settle, st.pos_settle, st.usable, st.note]
                          if st else [""] * 12))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n".join(__doc__.splitlines()[2:]))
    parser.add_argument("csv", type=Path, help="samples CSV from tools/export_history.py")
    g = parser.add_argument_group("columns (defaults: historian export names; '' disables optional ones)")
    for key in DEFAULT_COLUMNS:
        g.add_argument(f"--{key.replace('_', '-')}-col", dest=f"{key}_col", default=None,
                       metavar="NAME", help=f"default {DEFAULT_COLUMNS[key]}")
    a = parser.add_argument_group("analysis")
    a.add_argument("--final-window", type=float, default=30.0, help="s at the end of each hold (30)")
    a.add_argument("--min-hold", type=float, default=60.0, help="shorter holds are SHORT (60 s)")
    a.add_argument("--min-samples", type=int, default=5, help="valid flow samples in the window (5)")
    a.add_argument("--max-gap", type=float, default=30.0, help="larger time gaps split a hold (30 s)")
    a.add_argument("--cmd-tol", type=float, default=0.05, help="command change threshold (0.05 %%)")
    a.add_argument("--drift-tol", type=float, default=0.02,
                   help="flow change across the final window that counts as DRIFT (0.02 L/min)")
    a.add_argument("--drift-frac", type=float, default=0.05,
                   help="... or this fraction of the plateau flow, whichever is larger (0.05)")
    a.add_argument("--pos-drift-tol", type=float, default=1.0, help="position drift flag (1.0 %%)")
    a.add_argument("--min-response", type=float, default=0.05, help="min |dflow| for timing (0.05)")
    a.add_argument("--min-pos-change", type=float, default=0.5, help="min |dpos| for dF/dpos (0.5 %%)")
    a.add_argument("--settle-frac", type=float, default=0.05, help="settling band, fraction of step")
    a.add_argument("--settle-abs", type=float, default=0.02, help="min settling band (0.02 L/min)")
    a.add_argument("--pos-settle-abs", type=float, default=0.5, help="min position band (0.5 %%)")
    a.add_argument("--target", type=float, nargs=2, default=(0.3, 0.5), metavar=("LO", "HI"))
    a.add_argument("--grace", type=float, default=None,
                   help="s after a step for the start check (default treatment_stop.startup_grace_s)")
    a.add_argument("--any-mode", action="store_true", help="also analyse rows outside MANUAL")
    a.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS,
                   help="control_settings.json, read only, for the start check")
    a.add_argument("--no-settings", action="store_true", help="skip the treatment-start view")
    parser.add_argument("--out", type=Path, help="also write the plateau/step table as CSV")
    args = parser.parse_args(argv)

    settings = None if args.no_settings else read_settings(args.settings)
    if args.grace is None:
        args.grace = (settings or {}).get("startup_grace_s") or 10.0
    try:
        samples, cols, notes = load(args.csv, args)
        if settings is None and not args.no_settings:
            notes.append(f"settings not readable at {args.settings}: treatment-start view skipped")
        gaps = [b.t - a.t for a, b in zip(samples, samples[1:]) if b.boot == a.boot and b.t > a.t]
        dt = statistics.median(gaps) if gaps else None
        plateaus = segment(samples, args, notes)
        if not plateaus:
            raise AnalysisError("no rows with a Y4 command"
                                + ("" if args.any_mode else " in MANUAL (try --any-mode)"))
        for p in plateaus:
            characterise(p, args, dt)
        steps = [analyse_step(p, args) for p in plateaus if p.prev_plateau is not None]
    except AnalysisError as error:
        print(f"analyze_y4_steps: error: {error}", file=sys.stderr)
        return 2
    report(args.csv, samples, cols, notes, plateaus, steps, dt, args, settings)
    if args.out:
        write_out(args.out, plateaus, steps)
        print(f"plateau/step table written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
