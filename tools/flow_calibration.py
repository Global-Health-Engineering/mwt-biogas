"""Flow-sensor K-factor calibration (pulses per litre). Writes outputs for sensors 1 and 2.

    python3 tools/flow_calibration.py                        # asks which sensor
    python3 tools/flow_calibration.py --sensor flow_sensor_1
    python3 tools/flow_calibration.py --simulate             # desk rehearsal, no hardware

Settings: config/flow_calibration_settings.json (not loaded by the controller).

Transfer sensors (flow_sensor_1, flow_sensor_2), one run:
    counting starts -> pre_start_count_s later the transfer pump starts ->
    the pump stops after pump_runtime_s (earlier if the source tank goes low) ->
    counting continues through the rundown until no pulse for rundown_quiet_s ->
    you press Enter once nothing more runs into the bucket -> counting stops.
    The whole collected transfer, rundown included, is counted. Pump runtime
    only says how long the pump was commanded on, not the collection period.

Pasteuriser sensor (flow_sensor_3), one run (no output is written):
    Enter starts counting -> you open the valve by hand, let it run, close it ->
    Enter once nothing more flows -> counting stops.

After each run you enter the measured volume [L], the net liquid mass [kg,
bucket excluded] and an optional note. Measured volume is the calibration
reference; density = mass / volume is only a plausibility check.

    K_run      = pulse difference / measured volume           [pulses/L]
    K_combined = sum(pulses) / sum(volume), accepted runs only (no offset)
    volume [L] = pulses / K
    flow [L/min] = (pulses in interval / K) x 60 / interval_seconds

Every run is kept: accepted, excluded (with reason), repeated or aborted.
Session export (never overwritten), plus an SVG plot with the same name:
    exports/flow_calibration/<sensor>_calibration_<UTC>Z.csv
A per-run journal (logs/flow_calibration/) survives a crash mid-session.

The new factor is written to config/flow_settings.json only after you type
the sensor name. The previous file is backed up (flow_settings.json.<UTC>.bak),
the change is appended to config/flow_calibration_history.json, and
calibration_confirmed is set to false: confirm the factor by hand afterwards.

Safety (sensors 1 and 2): holds logs/controller.lock, so it refuses to run
alongside the controller and the controller cannot start meanwhile. Outputs
are written only through HardwareOutputWriter as complete frames: the selected
transfer pump, everything else (heaters included) off. All off on exit,
Ctrl+C or any error. The pump also stops if the source low switch is not
reached or the reading is unavailable. Destination levels are not checked:
the liquid is caught in a bucket before the buffer tank.
"""

import argparse
import copy
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.config import load_config  # noqa: E402
from pasteurizer.levels import LevelProcessor  # noqa: E402
from pasteurizer.output_commands import OutputCommands  # noqa: E402
from pasteurizer.run_lock import RunLock  # noqa: E402

COUNTER_MASK = 0xFFFFFFFF
COUNTER_HALF = 0x80000000          # a larger modular step is a reset/discontinuity
TRANSFER_COMMANDS = ("transfer_pump_1", "transfer_pump_2")
TRANSFER_SENSORS = ("flow_sensor_1", "flow_sensor_2")
MODE_CONTACTS = ("manual_switch", "auto_switch")

# Flags that make K unusable: such a run can only be repeated or excluded.
INVALID_FLAGS = {"NO_PULSES", "COUNTER_DISCONTINUITY", "DIO_INPUT_FAULT",
                 "HARDWARE_FAULT", "INVALID_VOLUME"}

FLAG_TEXT = {
    "NO_PULSES": "no pulses counted",
    "LOW_PULSES": "fewer pulses than minimum_pulses_per_run (quantisation)",
    "COUNTER_DISCONTINUITY": "counter jumped backwards/reset during the window",
    "DIO_INPUT_FAULT": "DIO input fault bits set during the window",
    "HARDWARE_FAULT": "hardware read/write failed during the window",
    "INVALID_VOLUME": "measured volume missing or not positive",
    "SAMPLING_GAP": "a gap between samples exceeded max_sample_gap_warning_s",
    "DENSITY_IMPLAUSIBLE": "mass/volume outside density_plausible_kg_per_l",
    "NO_MASS": "no mass entered; density not checked",
    "PULSES_BEFORE_PUMP_START": "pulses counted before the pump was started",
    "PUMP_STOPPED_EARLY": "pump stopped before its runtime (see pump_stop_reason)",
    "RUNDOWN_TIMEOUT": "pulses still arriving after max_rundown_wait_s",
    "STILL_FLOWING_AT_END": "last pulse shortly before the window was closed",
}


def utc_now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stamp(moment):
    return moment.strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TransferSettings:
    pump_command: str
    source_low_level: str          # level role, e.g. line_1_tank_low
    source_low_switch: str         # level switch input, e.g. level_switch_2
    pump_runtime_s: float
    planned_runs: int


@dataclass(frozen=True)
class CalibrationSettings:
    sample_interval_s: float
    pre_start_count_s: float
    rundown_quiet_s: float
    max_rundown_wait_s: float
    end_quiet_warning_s: float
    max_sample_gap_warning_s: float
    max_pump_runtime_s: float
    minimum_pulses_per_run: int
    deviation_warning_percent: float
    density_range: tuple[float, float]
    transfer: dict[str, TransferSettings]
    manual_window: tuple[str, ...]

    @property
    def sensors(self):
        return tuple(self.transfer) + self.manual_window


def _positive(data, key, maximum=None, integer=False):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"flow_calibration_settings.json: {key} must be a number")
    if integer and int(value) != value:
        raise ValueError(f"flow_calibration_settings.json: {key} must be an integer")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"flow_calibration_settings.json: {key} must be positive")
    if maximum is not None and value > maximum:
        raise ValueError(f"flow_calibration_settings.json: {key} must be <= {maximum}")
    return int(value) if integer else float(value)


def load_calibration_settings(path, plant):
    with open(path, encoding="utf-8") as file:
        data = json.load(file)

    max_runtime = _positive(data, "max_pump_runtime_s", maximum=600.0)
    density = data.get("density_plausible_kg_per_l")
    if (not isinstance(density, list) or len(density) != 2
            or not all(isinstance(v, (int, float)) for v in density)
            or not 0 < density[0] < density[1]):
        raise ValueError("density_plausible_kg_per_l must be [low, high] with 0 < low < high")

    counters = plant.io_mapping["flow_counters"]
    transfer = {}
    for sensor, options in data.get("transfer", {}).items():
        where = f"transfer.{sensor}"
        if sensor not in counters:
            raise ValueError(f"{where}: no flow counter in io_mapping.json")
        command = options.get("pump_command")
        if command not in TRANSFER_COMMANDS:
            raise ValueError(f"{where}.pump_command must be one of {TRANSFER_COMMANDS}")
        role = options.get("source_low_level")
        switch = plant.level_roles.get(role)
        if switch is None:
            raise ValueError(f"{where}.source_low_level {role!r} is not a level role")
        if not plant.levels[switch].enabled:
            raise ValueError(f"{where}: {switch} ({role}) is disabled in level_settings.json")
        runtime = _positive(options, "pump_runtime_s")
        if runtime > max_runtime:
            raise ValueError(f"{where}.pump_runtime_s exceeds max_pump_runtime_s ({max_runtime})")
        transfer[sensor] = TransferSettings(
            pump_command=command, source_low_level=role, source_low_switch=switch,
            pump_runtime_s=runtime,
            planned_runs=_positive(options, "planned_runs", maximum=50, integer=True),
        )
    if len({t.pump_command for t in transfer.values()}) != len(transfer):
        raise ValueError("Two transfer sensors use the same pump")

    manual = tuple(data.get("manual_window", {}))
    for sensor in manual:
        if sensor not in counters:
            raise ValueError(f"manual_window.{sensor}: no flow counter in io_mapping.json")
        if sensor in transfer:
            raise ValueError(f"{sensor} is listed as transfer and manual_window")

    return CalibrationSettings(
        sample_interval_s=_positive(data, "sample_interval_s", maximum=1.0),
        pre_start_count_s=_positive(data, "pre_start_count_s", maximum=30.0),
        rundown_quiet_s=_positive(data, "rundown_quiet_s", maximum=120.0),
        max_rundown_wait_s=_positive(data, "max_rundown_wait_s", maximum=1800.0),
        end_quiet_warning_s=_positive(data, "end_quiet_warning_s", maximum=60.0),
        max_sample_gap_warning_s=_positive(data, "max_sample_gap_warning_s", maximum=10.0),
        max_pump_runtime_s=max_runtime,
        minimum_pulses_per_run=_positive(data, "minimum_pulses_per_run", integer=True),
        deviation_warning_percent=_positive(data, "deviation_warning_percent", maximum=100.0),
        density_range=(float(density[0]), float(density[1])),
        transfer=transfer,
        manual_window=manual,
    )


# ---------------------------------------------------------------------------
# Hardware access (only the sampler thread calls read() and write())
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sample:
    t: float                        # monotonic seconds
    counter: int                    # raw counter value
    dio_status: int
    inputs: dict[str, bool]


class HardwareIO:
    """HardwareReader (read-only) plus, for transfer runs, HardwareOutputWriter."""

    def __init__(self, plant, sensor, write_outputs):
        from pasteurizer.hardware import HardwareReader      # needs revpimodio2

        mapping = copy.deepcopy(plant.io_mapping)
        used = set(MODE_CONTACTS) | {n for n, o in plant.levels.items() if o.enabled}
        mapping["digital_inputs"] = {n: c for n, c in mapping["digital_inputs"].items()
                                     if n in used}
        self.sensor = sensor
        self.reader = HardwareReader(mapping=mapping)
        self.writer = None
        if write_outputs:
            try:
                from pasteurizer.hardware_outputs import HardwareOutputWriter
                self.writer = HardwareOutputWriter(plant)     # writes all off at once
            except Exception:
                self.reader.close()
                raise

    def read(self):
        s = self.reader.read()
        return Sample(t=s["sampled_at"], counter=s["flow_counters"][self.sensor],
                      dio_status=s["module_status"]["dio"], inputs=s["digital_inputs"])

    def write(self, frame):
        return True if self.writer is None else self.writer.write(frame)

    def close(self):
        try:
            if self.writer is not None:
                self.writer.close()                           # all off
        finally:
            self.reader.close()


class SimulatedIO:
    """Desk rehearsal: a counter that responds to the pump frame. No hardware."""

    TRUE_K = 476.0                  # pulses per litre of the simulated sensor
    PUMP_L_MIN = 6.0
    RUNDOWN_TAU_S = 1.5
    MANUAL_DELAY_S, MANUAL_FLOW_S, MANUAL_L_MIN = 1.0, 4.0, 1.5

    def __init__(self, plant, sensor, write_outputs):
        self.sensor = sensor
        self.counter = 0xFFFFFF00   # close to rollover on purpose
        self._fraction = 0.0
        self._flow = 0.0
        self._pump = False
        self._last = time.monotonic()
        self._manual_start = None
        low_switches = {plant.level_roles[r] for r in plant.level_roles if r.endswith("_tank_low")}
        self.inputs = {n: n in low_switches for n in plant.io_mapping["digital_inputs"]}

    def window_started(self):
        self._manual_start = time.monotonic()

    def read(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self._pump:
            self._flow = self.PUMP_L_MIN
        elif self._manual_start is not None:
            age = now - self._manual_start
            on = self.MANUAL_DELAY_S <= age < self.MANUAL_DELAY_S + self.MANUAL_FLOW_S
            self._flow = self.MANUAL_L_MIN if on else 0.0
        else:
            self._flow *= math.exp(-dt / self.RUNDOWN_TAU_S)
            if self._flow < 0.02:
                self._flow = 0.0
        self._fraction += self._flow / 60.0 * dt * self.TRUE_K
        whole = int(self._fraction)
        self._fraction -= whole
        self.counter = (self.counter + whole) & COUNTER_MASK
        return Sample(t=now, counter=self.counter, dio_status=0, inputs=dict(self.inputs))

    def write(self, frame):
        self._pump = any(getattr(frame, c) for c in TRANSFER_COMMANDS)
        return True

    def close(self):
        self._pump = False


# ---------------------------------------------------------------------------
# Sampler: counts pulses, owns the pump command
# ---------------------------------------------------------------------------

@dataclass
class Window:
    start_t: float
    start_utc: datetime
    start_raw: int
    end_raw: int
    pulses: int = 0
    pulses_before_pump: int = 0
    first_pulse_t: float | None = None
    last_pulse_t: float | None = None
    last_sample_t: float | None = None
    max_gap_s: float = 0.0
    discontinuity: bool = False
    input_fault: bool = False
    hardware_fault: bool = False
    pump_start_t: float | None = None
    pump_stop_t: float | None = None
    pump_stop_reason: str | None = None
    commanded_runtime_s: float | None = None


@dataclass
class WindowResult:
    start_utc: datetime
    end_utc: datetime
    counter_start_raw: int
    counter_end_raw: int
    pulses: int
    pulses_before_pump: int | None
    duration_s: float
    active_duration_s: float | None
    seconds_since_last_pulse: float | None
    max_gap_s: float
    discontinuity: bool
    input_fault: bool
    hardware_fault: bool
    commanded_runtime_s: float | None
    pump_on_command_s: float | None
    pump_stop_reason: str | None


class Sampler(threading.Thread):
    def __init__(self, io, interval_s, pump_command=None, level_switch=None, level_settings=None):
        super().__init__(daemon=True, name="flow-calibration-sampler")
        self.io = io
        self.interval_s = interval_s
        self.pump_command = pump_command
        self.level_switch = level_switch
        self._level = (LevelProcessor(level_settings.active_high, level_settings.debounce_seconds)
                       if level_switch else None)
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self.ready = threading.Event()
        self.fault = None
        self.level_reached = None
        self.rate_hz = None
        self._prev_raw = None
        self._prev_t = None
        self._window = None
        self._pump_on = False
        self._pump_deadline = None

    # --- thread ---------------------------------------------------------

    def run(self):
        while not self._halt.is_set():
            started = time.monotonic()
            try:
                sample = self.io.read()
            except Exception as error:                  # noqa: BLE001
                self._hardware_fault(f"read failed: {error}")
            else:
                self._process(sample)
            self._write()
            self.ready.set()
            self._halt.wait(max(0.0, self.interval_s - (time.monotonic() - started)))
        with self._lock:
            self._pump_on = False
        self._write()

    def _hardware_fault(self, message):
        with self._lock:
            self.fault = message
            if self._pump_on:
                self._stop_pump_locked(time.monotonic(), f"hardware fault: {message}")
            if self._window is not None:
                self._window.hardware_fault = True

    def _process(self, sample):
        raw = int(sample.counter) & COUNTER_MASK
        with self._lock:
            if self._prev_raw is not None:
                gap = sample.t - self._prev_t
                delta = (raw - self._prev_raw) & COUNTER_MASK
                jump = delta >= COUNTER_HALF
                if jump:
                    delta = 0
                self.rate_hz = delta / gap if gap > 0 else None
                w = self._window
                if w is not None:
                    w.max_gap_s = max(w.max_gap_s, gap)
                    w.discontinuity |= jump
                    w.pulses += delta
                    if delta and w.pump_start_t is None and w.commanded_runtime_s is not None:
                        w.pulses_before_pump += delta
                    if delta:
                        w.first_pulse_t = w.first_pulse_t or sample.t
                        w.last_pulse_t = sample.t
                    w.end_raw = raw
                    w.last_sample_t = sample.t
            if sample.dio_status & 0x00FF and self._window is not None:
                self._window.input_fault = True
            self._prev_raw, self._prev_t = raw, sample.t

            if self._level is not None:
                value = sample.inputs.get(self.level_switch)
                if value is None:
                    self._level.reset()
                    self.level_reached = None
                else:
                    self.level_reached = self._level.update(value, sample.t).reached

            if self._pump_on:
                if self.level_reached is not True:
                    reason = ("source low level not reached" if self.level_reached is False
                              else "source level reading unavailable")
                    self._stop_pump_locked(sample.t, reason)
                elif sample.t >= self._pump_deadline:
                    self._stop_pump_locked(sample.t, "runtime complete")

    def _write(self):
        if self.pump_command is None:
            return                                      # manual window: never writes
        with self._lock:
            frame = OutputCommands(**{self.pump_command: self._pump_on})
        try:
            ok = self.io.write(frame)
        except Exception as error:                      # noqa: BLE001
            ok, message = False, f"write failed: {error}"
        else:
            message = "output write not accepted"
        if not ok:
            self._hardware_fault(message)
            try:
                self.io.write(OutputCommands())
            except Exception:                           # noqa: BLE001
                pass

    def _stop_pump_locked(self, t, reason):
        self._pump_on = False
        w = self._window
        if w is not None and w.pump_stop_t is None:
            w.pump_stop_t = t
            w.pump_stop_reason = reason

    # --- main-thread interface ------------------------------------------

    def start_window(self, commanded_runtime_s=None):
        with self._lock:
            if self.fault:
                raise RuntimeError(f"Hardware fault: {self.fault}")
            self._window = Window(start_t=self._prev_t, start_utc=utc_now(),
                                  start_raw=self._prev_raw, end_raw=self._prev_raw,
                                  commanded_runtime_s=commanded_runtime_s)
        hook = getattr(self.io, "window_started", None)
        if hook is not None and commanded_runtime_s is None:
            hook()

    def start_pump(self, runtime_s):
        with self._lock:
            if self.fault:
                return f"hardware fault: {self.fault}"
            if self.level_reached is not True:
                return "source low level not reached"
            now = time.monotonic()
            self._pump_on = True
            self._pump_deadline = now + runtime_s
            if self._window is not None:
                self._window.pump_start_t = now
        return None

    def stop_pump(self, reason):
        with self._lock:
            if self._pump_on:
                self._stop_pump_locked(time.monotonic(), reason)

    @property
    def pump_on(self):
        with self._lock:
            return self._pump_on

    def status(self):
        with self._lock:
            w = self._window
            now = self._prev_t
            return {
                "pulses": None if w is None else w.pulses,
                "elapsed_s": None if w is None or now is None else now - w.start_t,
                "quiet_s": (None if w is None or now is None
                            else now - (w.last_pulse_t if w.last_pulse_t else w.start_t)),
                "rate_hz": self.rate_hz,
                "pump_on": self._pump_on,
                "level": self.level_reached,
            }

    def stop_window(self):
        with self._lock:
            w, self._window = self._window, None
            if self._pump_on:
                self._pump_on = False
            end_t = w.last_sample_t if w.last_sample_t is not None else w.start_t
        if w.pump_start_t is not None and w.pump_stop_t is None:
            w.pump_stop_t, w.pump_stop_reason = end_t, "window closed"
        if w.commanded_runtime_s is not None and w.pump_start_t is None:
            w.pump_stop_reason = w.pump_stop_reason or "pump not started"
        active = (w.last_pulse_t - w.first_pulse_t
                  if w.first_pulse_t is not None and w.last_pulse_t > w.first_pulse_t else None)
        return WindowResult(
            start_utc=w.start_utc, end_utc=utc_now(),
            counter_start_raw=w.start_raw, counter_end_raw=w.end_raw,
            pulses=w.pulses,
            pulses_before_pump=w.pulses_before_pump if w.commanded_runtime_s is not None else None,
            duration_s=end_t - w.start_t,
            active_duration_s=active,
            seconds_since_last_pulse=None if w.last_pulse_t is None else end_t - w.last_pulse_t,
            max_gap_s=w.max_gap_s,
            discontinuity=w.discontinuity, input_fault=w.input_fault,
            hardware_fault=w.hardware_fault,
            commanded_runtime_s=w.commanded_runtime_s,
            pump_on_command_s=(None if w.pump_start_t is None
                               else w.pump_stop_t - w.pump_start_t),
            pump_stop_reason=w.pump_stop_reason,
        )

    def close(self):
        self._halt.set()
        self.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Runs and evaluation (pure functions)
# ---------------------------------------------------------------------------

def build_record(sensor, run_number, result, volume_l, mass_kg, note, settings):
    flags = []
    if result.pulses == 0:
        flags.append("NO_PULSES")
    elif result.pulses < settings.minimum_pulses_per_run:
        flags.append("LOW_PULSES")
    if result.discontinuity:
        flags.append("COUNTER_DISCONTINUITY")
    if result.input_fault:
        flags.append("DIO_INPUT_FAULT")
    if result.hardware_fault:
        flags.append("HARDWARE_FAULT")
    if result.max_gap_s > settings.max_sample_gap_warning_s:
        flags.append("SAMPLING_GAP")
    if volume_l is None or not volume_l > 0:
        flags.append("INVALID_VOLUME")
    density = None
    if mass_kg is None:
        flags.append("NO_MASS")
    elif volume_l and volume_l > 0:
        density = mass_kg / volume_l
        low, high = settings.density_range
        if not low <= density <= high:
            flags.append("DENSITY_IMPLAUSIBLE")
    if result.pulses_before_pump:
        flags.append("PULSES_BEFORE_PUMP_START")
    if result.pump_stop_reason not in (None, "runtime complete"):
        flags.append("PUMP_STOPPED_EARLY")
    if (result.seconds_since_last_pulse is not None
            and result.seconds_since_last_pulse < settings.end_quiet_warning_s):
        flags.append("STILL_FLOWING_AT_END")

    k = (result.pulses / volume_l
         if result.pulses > 0 and volume_l and volume_l > 0 else None)

    def per_min(seconds):
        return volume_l / seconds * 60.0 if volume_l and seconds and seconds > 0 else None

    return {
        "run_number": run_number,
        "sensor_id": sensor,
        "timestamp_utc": iso(result.start_utc),
        "window_end_utc": iso(result.end_utc),
        "status": "pending",
        "exclusion_reason": "",
        "status_log": "",
        "counter_start_raw": result.counter_start_raw,
        "counter_end_raw": result.counter_end_raw,
        "pulse_difference": result.pulses,
        "counter_end_minus_start_mod32": (result.counter_end_raw - result.counter_start_raw)
        & COUNTER_MASK,
        "pulses_before_pump_start": result.pulses_before_pump,
        "measurement_duration_s": result.duration_s,
        "first_to_last_pulse_s": result.active_duration_s,
        "max_sample_gap_s": result.max_gap_s,
        "commanded_pump_runtime_s": result.commanded_runtime_s,
        "pump_on_command_s": result.pump_on_command_s,
        "pump_stop_reason": result.pump_stop_reason or "",
        "measured_volume_l": volume_l,
        "net_mass_kg": mass_kg,
        "density_kg_per_l": density,
        "k_pulses_per_l": k,
        "k_deviation_percent": None,
        "window_avg_flow_l_min_incl_waiting": per_min(result.duration_s),
        "first_to_last_pulse_avg_flow_l_min": per_min(result.active_duration_s),
        "flags": flags,
        "note": note,
    }


def is_valid(record):
    return record["k_pulses_per_l"] is not None and not (set(record["flags"]) & INVALID_FLAGS)


def set_status(record, status, reason=""):
    record["status"] = status
    record["exclusion_reason"] = reason if status != "accepted" else ""
    entry = f"{iso(utc_now())} {status}" + (f" ({reason})" if reason else "")
    record["status_log"] = (record["status_log"] + " | " if record["status_log"] else "") + entry


def evaluate(runs, warn_percent):
    accepted = [r for r in runs if r["status"] == "accepted"]
    pulses = sum(r["pulse_difference"] for r in accepted)
    volume = sum(r["measured_volume_l"] for r in accepted)
    k = pulses / volume if accepted and pulses > 0 and volume > 0 else None
    inconsistent = []
    for r in runs:
        r["k_deviation_percent"] = (
            (r["k_pulses_per_l"] - k) / k * 100.0
            if k is not None and r["k_pulses_per_l"] is not None else None)
        if (r["status"] == "accepted" and r["k_deviation_percent"] is not None
                and abs(r["k_deviation_percent"]) > warn_percent):
            inconsistent.append(r["run_number"])
    ks = [r["k_pulses_per_l"] for r in accepted]
    mean = sum(ks) / len(ks) if ks else None
    std = (math.sqrt(sum((x - mean) ** 2 for x in ks) / (len(ks) - 1))
           if len(ks) > 1 else None)
    return {
        "accepted_runs": len(accepted),
        "sum_pulses": pulses,
        "sum_volume_l": volume,
        "k_combined": k,
        "k_run_mean": mean,
        "k_run_std": std,
        "k_run_cv_percent": None if std is None or not mean else std / mean * 100.0,
        "max_abs_deviation_percent": max(
            (abs(r["k_deviation_percent"]) for r in accepted
             if r["k_deviation_percent"] is not None), default=None),
        "inconsistent_runs": inconsistent,
    }


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

RUN_COLUMNS = (
    "timestamp_utc", "window_end_utc", "sensor_id", "run_number", "status",
    "exclusion_reason", "status_log", "counter_start_raw", "counter_end_raw",
    "pulse_difference", "counter_end_minus_start_mod32", "pulses_before_pump_start",
    "measurement_duration_s", "first_to_last_pulse_s", "max_sample_gap_s",
    "commanded_pump_runtime_s", "pump_on_command_s", "pump_stop_reason",
    "measured_volume_l", "net_mass_kg", "density_kg_per_l", "k_pulses_per_l",
    "k_deviation_percent", "window_avg_flow_l_min_incl_waiting",
    "first_to_last_pulse_avg_flow_l_min", "flags", "note",
)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    return value


def exclusive_path(directory, stem, suffix):
    """A path that does not exist yet (never overwrite an earlier export)."""
    directory.mkdir(parents=True, exist_ok=True)
    path, n = directory / f"{stem}{suffix}", 2
    while path.exists():
        path, n = directory / f"{stem}_{n}{suffix}", n + 1
    return path


def write_export(path, runs, summary, info):
    """Runs table, blank line, then a parameter/value summary section."""
    with open(path, "x", newline="", encoding="utf-8") as file:
        out = csv.writer(file)
        out.writerow(RUN_COLUMNS)
        for r in runs:
            out.writerow([_cell(r[c]) for c in RUN_COLUMNS])
        out.writerow([])
        out.writerow(["summary_parameter", "value"])
        for key, value in {**info, **summary}.items():
            out.writerow([key, _cell(value)])


def _nice_step(span, target=6):
    raw = span / target
    power = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * power >= raw:
            return m * power
    return 10 * power


def _axis(lo, hi):
    if hi <= lo:
        hi = lo + 1.0
    step = _nice_step(hi - lo)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 1e-9:
        ticks.append(v)
        v += step
    if ticks[-1] < hi:
        ticks.append(ticks[-1] + step)
    return ticks


def _fmt(v):
    return f"{v:g}" if abs(v) < 1e5 else f"{v:.3g}"


def _panel(x0, y0, w, h, points, xlabel, ylabel, title, line=None, hline=None, band=None):
    """points: (x, y, label, kind) with kind accepted/flagged/excluded."""
    xs = [p[0] for p in points] or [1.0]
    ys = [p[1] for p in points] or [1.0]
    xlo = 0.0 if line else min(xs) * 0.9
    xt = _axis(xlo, max(xs) * 1.1)
    if line:
        ys.append(line(xt[-1]))             # the line reaches the right edge inside the frame
    if hline is not None:
        ys.append(hline * (1 + (band or 0) / 100))
    ylo = 0.0 if line else min(min(ys) * 0.97, hline * (1 - (band or 0) / 100) if hline else 1e18)
    yt = _axis(ylo, max(ys) * 1.05)
    left, top, pw, ph = x0 + 70, y0 + 40, w - 100, h - 100

    def sx(v):
        return left + (v - xt[0]) / (xt[-1] - xt[0]) * pw

    def sy(v):
        return top + ph - (v - yt[0]) / (yt[-1] - yt[0]) * ph

    s = [f'<text x="{x0 + w / 2}" y="{y0 + 22}" text-anchor="middle" font-size="15" '
         f'font-weight="600">{title}</text>',
         f'<rect x="{left}" y="{top}" width="{pw}" height="{ph}" fill="none" stroke="#999"/>']
    for v in xt:
        s.append(f'<line x1="{sx(v):.1f}" y1="{top}" x2="{sx(v):.1f}" y2="{top + ph}" stroke="#eee"/>')
        s.append(f'<text x="{sx(v):.1f}" y="{top + ph + 16}" text-anchor="middle" '
                 f'font-size="11">{_fmt(v)}</text>')
    for v in yt:
        s.append(f'<line x1="{left}" y1="{sy(v):.1f}" x2="{left + pw}" y2="{sy(v):.1f}" stroke="#eee"/>')
        s.append(f'<text x="{left - 6}" y="{sy(v) + 4:.1f}" text-anchor="end" '
                 f'font-size="11">{_fmt(v)}</text>')
    s.append(f'<text x="{left + pw / 2}" y="{top + ph + 36}" text-anchor="middle" '
             f'font-size="12">{xlabel}</text>')
    s.append(f'<text x="{x0 + 16}" y="{top + ph / 2}" text-anchor="middle" font-size="12" '
             f'transform="rotate(-90 {x0 + 16} {top + ph / 2})">{ylabel}</text>')
    if line:
        s.append(f'<line x1="{sx(0):.1f}" y1="{sy(line(0)):.1f}" x2="{sx(xt[-1]):.1f}" '
                 f'y2="{sy(line(xt[-1])):.1f}" stroke="#1f6feb" stroke-width="1.5"/>')
    if hline is not None:
        if band:
            for f in (1 + band / 100, 1 - band / 100):
                s.append(f'<line x1="{left}" y1="{sy(hline * f):.1f}" x2="{left + pw}" '
                         f'y2="{sy(hline * f):.1f}" stroke="#1f6feb" stroke-dasharray="4 4"/>')
        s.append(f'<line x1="{left}" y1="{sy(hline):.1f}" x2="{left + pw}" y2="{sy(hline):.1f}" '
                 f'stroke="#1f6feb" stroke-width="1.5"/>')
    style = {"accepted": 'fill="#1f6feb"', "flagged": 'fill="#d97706"',
             "excluded": 'fill="white" stroke="#888" stroke-width="1.5"'}
    for i, (x, y, label, kind) in enumerate(points):
        s.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5" {style[kind]}/>')
        # Runs at the same runtime cluster: stagger labels so they stay readable.
        dy = 6 + 12 * (i % 8)
        s.append(f'<line x1="{sx(x):.1f}" y1="{sy(y):.1f}" x2="{sx(x) + 8:.1f}" '
                 f'y2="{sy(y) - dy + 3:.1f}" stroke="#bbb"/>')
        s.append(f'<text x="{sx(x) + 9:.1f}" y="{sy(y) - dy:.1f}" font-size="11">{label}</text>')
    return s


def write_plot(path, sensor, runs, summary, warn_percent):
    k = summary["k_combined"]
    inconsistent = set(summary["inconsistent_runs"])

    def kind(r):
        if r["status"] != "accepted":
            return "excluded"
        return "flagged" if r["run_number"] in inconsistent else "accepted"

    usable = [r for r in runs if r["measured_volume_l"] and r["measured_volume_l"] > 0]
    pts = [(r["measured_volume_l"], r["pulse_difference"], f"#{r['run_number']}", kind(r))
           for r in usable]
    k_text = f"K = {k:.5g} pulses/L" if k else "no accepted runs"
    width, height = 760, 480
    body = _panel(0, 0, width, height, pts, "measured volume [L]", "pulse difference",
                  f"{sensor}: pulses vs volume ({k_text}, line pulses = K x volume)",
                  line=(lambda v: k * v) if k else None)
    flow_pts = [(r["first_to_last_pulse_avg_flow_l_min"], r["k_pulses_per_l"],
                 f"#{r['run_number']}", kind(r))
                for r in runs if r["first_to_last_pulse_avg_flow_l_min"] and r["k_pulses_per_l"]]
    if sensor in TRANSFER_SENSORS:
        run_pts = [(r["run_number"], r["k_pulses_per_l"], f"#{r['run_number']}", kind(r))
                   for r in runs if r["k_pulses_per_l"]]
        if run_pts:
            body += _panel(0, height, width, height, run_pts, "run number", "K [pulses/L]",
                           f"{sensor}: K per run (band +/-{warn_percent:g} %)",
                           hline=k, band=warn_percent if k else None)
            height *= 2
    elif flow_pts:
        body += _panel(0, height, width, height, flow_pts,
                       "average flow, first to last pulse [L/min] (manual waiting excluded)",
                       "K [pulses/L]",
                       f"{sensor}: K vs average flow (band +/-{warn_percent:g} %)",
                       hline=k, band=warn_percent if k else None)
        height *= 2
    legend_y = height - 12
    body.append(f'<text x="70" y="{legend_y}" font-size="11">blue: accepted   orange: accepted, '
                f'deviation above {warn_percent:g} %   hollow: excluded / repeated / aborted</text>')
    with open(path, "x", encoding="utf-8") as file:
        file.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                   f'font-family="sans-serif"><rect width="100%" height="100%" fill="white"/>'
                   + "".join(body) + "</svg>\n")


def git_commit():
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return out.stdout.strip() + ("+uncommitted" if dirty else "") if out.returncode == 0 else ""
    except Exception:                                   # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Saving the factor
# ---------------------------------------------------------------------------

def _atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def save_factor(config_dir, sensor, k, export_path, summary):
    path = config_dir / "flow_settings.json"
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    previous = dict(data[sensor])
    now = utc_now()
    backup = exclusive_path(config_dir, f"flow_settings.json.{stamp(now)}", ".bak")
    with open(backup, "x", encoding="utf-8") as file:
        file.write(original)

    new_k = float(f"{k:.6g}")
    data[sensor]["pulses_per_litre"] = new_k
    data[sensor]["calibration_confirmed"] = False
    _atomic_write(path, json.dumps(data, indent=2) + "\n")
    try:
        load_config(config_dir)                         # same validation as the controller
    except Exception:
        _atomic_write(path, original)
        raise

    history_path = config_dir / "flow_calibration_history.json"
    history = (json.loads(history_path.read_text(encoding="utf-8"))
               if history_path.exists() else [])
    history.append({
        "saved_at_utc": iso(now),
        "sensor_id": sensor,
        "previous_pulses_per_litre": previous["pulses_per_litre"],
        "previous_calibration_confirmed": previous["calibration_confirmed"],
        "new_pulses_per_litre": new_k,
        "calibration_confirmed_set_to": False,
        "accepted_runs": summary["accepted_runs"],
        "max_abs_deviation_percent": summary["max_abs_deviation_percent"],
        "source_export": str(export_path.relative_to(ROOT)) if export_path.is_relative_to(ROOT)
        else str(export_path),
        "backup_file": backup.name,
        "software_commit": git_commit(),
    })
    _atomic_write(history_path, json.dumps(history, indent=2) + "\n")
    return new_k, previous, backup


# ---------------------------------------------------------------------------
# Terminal interaction
# ---------------------------------------------------------------------------

def ask(prompt):
    return input(prompt).strip()


def ask_number(prompt, required=True):
    while True:
        text = ask(prompt).replace(",", ".")
        if not text and not required:
            return None
        try:
            value = float(text)
        except ValueError:
            print("  Please enter a number (e.g. 1.25).")
            continue
        if not math.isfinite(value) or value <= 0:
            print("  The value must be positive.")
            continue
        return value


def ask_yes(prompt, default=False):
    answer = ask(prompt).lower()
    return default if not answer else answer in ("y", "yes", "j", "ja")


def status_line(sampler):
    s = sampler.status()
    rate = "-" if s["rate_hz"] is None else f"{s['rate_hz']:.1f}"
    parts = [f"pulses={s['pulses']}", f"rate={rate} p/s",
             f"t={s['elapsed_s'] or 0:.1f} s", f"quiet={s['quiet_s'] or 0:.1f} s"]
    if sampler.pump_command:
        parts += [f"pump={'ON' if s['pump_on'] else 'off'}",
                  f"source_low={ {True: 'reached', False: 'NOT reached', None: '?'}[s['level']]}"]
    return "  " + "  ".join(parts)


def show_live(sampler, stop):
    while not stop.wait(1.0):
        print("\r" + status_line(sampler) + "    ", end="", flush=True)


def wait_enter_live(sampler, prompt):
    """Show live counts while waiting for Enter (counting goes on meanwhile)."""
    print(prompt, flush=True)
    stop = threading.Event()
    thread = threading.Thread(target=show_live, args=(sampler, stop), daemon=True)
    thread.start()
    try:
        input()
    finally:
        stop.set()
        thread.join()
    print(status_line(sampler))


def check_fault(sampler):
    if sampler.fault:
        raise RuntimeError(f"Hardware fault: {sampler.fault}")


def transfer_window(sampler, cfg, settings):
    """Count one complete transfer, pump rundown included. KeyboardInterrupt aborts."""
    sampler.start_window(commanded_runtime_s=cfg.pump_runtime_s)
    try:
        print(f"  Counting. Pump starts in {settings.pre_start_count_s:g} s ...")
        time.sleep(settings.pre_start_count_s)
        refused = sampler.start_pump(cfg.pump_runtime_s)
        if refused:
            print(f"  Pump NOT started: {refused}.")
        else:
            print(f"  Pump {cfg.pump_command} ON for {cfg.pump_runtime_s:g} s "
                  "(Ctrl+C stops it at once).")
        while sampler.pump_on:
            time.sleep(1.0)
            print("\r" + status_line(sampler) + "    ", end="", flush=True)
        print()
        print("  Pump off. Counting the rundown until no pulse for "
              f"{settings.rundown_quiet_s:g} s ...")
        rundown_start = time.monotonic()
        timeout = False
        while True:
            check_fault(sampler)
            status = sampler.status()
            if (status["quiet_s"] or 0) >= settings.rundown_quiet_s:
                break
            if time.monotonic() - rundown_start > settings.max_rundown_wait_s:
                timeout = True
                print("\n  WARNING: pulses still arriving after max_rundown_wait_s.")
                break
            time.sleep(0.5)
            print("\r" + status_line(sampler) + "    ", end="", flush=True)
        print()
        wait_enter_live(sampler, "  Press Enter once nothing more runs into the bucket "
                                 "(counting continues until then).")
    except BaseException:
        sampler.stop_pump("aborted by operator")
        result = sampler.stop_window()
        raise _Aborted(result) from None
    return sampler.stop_window(), timeout


class _Aborted(Exception):
    def __init__(self, result):
        super().__init__("run aborted")
        self.result = result


def manual_window(sampler):
    ask("  Press Enter to START the measurement window (counting begins), "
        "then open the valve: ")
    sampler.start_window()
    try:
        wait_enter_live(sampler, "  Window open. Open the valve, let it run, close it.\n"
                                 "  Press Enter to STOP once no further flow is coming.")
    except BaseException:
        raise _Aborted(sampler.stop_window()) from None
    return sampler.stop_window(), False


def enter_measurement(sampler_result):
    print(f"  Counted {sampler_result.pulses} pulses in "
          f"{sampler_result.duration_s:.1f} s.")
    volume = ask_number("  Measured volume [L]: ")
    mass = ask_number("  Net liquid mass [kg, bucket excluded] (Enter if not weighed): ",
                      required=False)
    note = ask("  Note (optional): ")
    return volume, mass, note


def print_run(record, runs, settings):
    k = record["k_pulses_per_l"]
    density = record["density_kg_per_l"]
    print(f"\n  Run {record['run_number']}: pulses={record['pulse_difference']}  "
          f"volume={record['measured_volume_l']:g} L  "
          f"K={'INVALID' if k is None else f'{k:.5g} pulses/L'}  "
          f"density={'-' if density is None else f'{density:.3f} kg/L'}")
    accepted = [r for r in runs if r["status"] == "accepted"]
    if accepted and k is not None:
        preview = evaluate(accepted + [dict(record, status="accepted")],
                           settings.deviation_warning_percent)
        print(f"  Combined K with this run: {preview['k_combined']:.5g} pulses/L; "
              f"this run deviates {(k - preview['k_combined']) / preview['k_combined'] * 100:+.2f} %")
    for flag in record["flags"]:
        mark = "INVALID" if flag in INVALID_FLAGS else "warning"
        print(f"  [{mark}] {flag}: {FLAG_TEXT[flag]}")


def decide(record, runs, settings, journal):
    """Returns the final status: accepted, excluded or repeated."""
    print_run(record, runs, settings)
    valid = is_valid(record)
    options = "[a]ccept, [r]epeat, [e]xclude" if valid else "[r]epeat, [e]xclude (K unusable)"
    while True:
        choice = ask(f"  {options}: ").lower()
        if choice == "a" and valid:
            set_status(record, "accepted")
            break
        if choice == "r":
            set_status(record, "repeated", ask("  Reason (optional): ") or "repeated by operator")
            break
        if choice == "e":
            reason = ""
            while not reason:
                reason = ask("  Exclusion reason (required): ")
            set_status(record, "excluded", reason)
            break
    journal.write("status", record)
    return record["status"]


class Journal:
    """Append-only JSON lines, flushed per record: nothing measured is lost."""

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "x", encoding="utf-8")

    def write(self, event, record):
        self.file.write(json.dumps({"event": event, "at_utc": iso(utc_now()), **record}) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


def print_table(runs, summary):
    print("\n  run  status     pulses   volume_L  mass_kg  dens_kg/L  K_p/L      dev_%    "
          "flow_L/min  flags")
    for r in runs:
        def f(key, spec):
            v = r[key]
            width = int(spec.lstrip("+").split(".")[0])
            return "-".rjust(width) if v is None else format(v, spec)
        mark = " <" if r["run_number"] in summary["inconsistent_runs"] else ""
        print(f"  {r['run_number']:>3}  {r['status']:<9}  {r['pulse_difference']:>7}  "
              f"{f('measured_volume_l', '8.3f')}  {f('net_mass_kg', '7.3f')}  "
              f"{f('density_kg_per_l', '9.3f')}  {f('k_pulses_per_l', '9.5g')}  "
              f"{f('k_deviation_percent', '+7.2f')}  "
              f"{f('first_to_last_pulse_avg_flow_l_min', '10.3f')}  "
              f"{','.join(r['flags'])}{mark}")
    k = summary["k_combined"]
    print(f"\n  Accepted runs: {summary['accepted_runs']}   sum pulses: {summary['sum_pulses']}   "
          f"sum volume: {summary['sum_volume_l']:.4g} L")
    print(f"  Proposed K = sum(pulses)/sum(volume) = "
          f"{'unavailable' if k is None else f'{k:.6g} pulses/L'}")
    if summary["k_run_cv_percent"] is not None:
        print(f"  Spread of run K: std {summary['k_run_std']:.4g} pulses/L "
              f"(CV {summary['k_run_cv_percent']:.2f} %), max |deviation| "
              f"{summary['max_abs_deviation_percent']:.2f} %")
    print("  flow_L/min = average from first to last pulse (manual waiting excluded).")
    if summary["inconsistent_runs"]:
        print(f"  '<' INCONSISTENT: runs {summary['inconsistent_runs']} deviate more than the "
              "warning limit. They are still accepted; exclude them only with a reason.")


def review(runs, settings, journal):
    while True:
        summary = evaluate(runs, settings.deviation_warning_percent)
        print_table(runs, summary)
        command = ask("\n  Enter to finish, 'x N' to exclude run N, 'i N' to include run N: ")
        if not command:
            return summary
        parts = command.split()
        if len(parts) != 2 or parts[0] not in ("x", "i") or not parts[1].isdigit():
            continue
        match = [r for r in runs if r["run_number"] == int(parts[1])]
        if not match:
            print("  No such run.")
            continue
        record = match[0]
        if parts[0] == "x":
            reason = ""
            while not reason:
                reason = ask("  Exclusion reason (required): ")
            set_status(record, "excluded", reason)
        elif not is_valid(record):
            print("  This run has no usable K and cannot be included.")
            continue
        else:
            set_status(record, "accepted", "")
            record["status_log"] += f" (re-included: {ask('  Reason for including: ')})"
        journal.write("status", record)


def choose_sensor(settings):
    sensors = settings.sensors
    print("Which flow sensor is calibrated?")
    for i, sensor in enumerate(sensors, 1):
        kind = (f"transfer, {settings.transfer[sensor].pump_command}"
                if sensor in settings.transfer else "pasteuriser, manual valve")
        print(f"  {i}: {sensor} ({kind})")
    while True:
        answer = ask("Number: ")
        if answer.isdigit() and 1 <= int(answer) <= len(sensors):
            return sensors[int(answer) - 1]


def run_session(sensor, plant, settings, simulate, config_dir):
    transfer = settings.transfer.get(sensor)
    session_start = utc_now()
    tag = "SIMULATED_" if simulate else ""
    journal = Journal(exclusive_path(ROOT / "logs" / "flow_calibration",
                                     f"{tag}{sensor}_journal_{stamp(session_start)}", ".jsonl"))
    old = plant.flows[sensor]
    print(f"\n{sensor}: current pulses_per_litre = {old.pulses_per_litre} "
          f"(calibration_confirmed = {old.calibration_confirmed})")
    if transfer:
        equipment = plant.command_outputs[transfer.pump_command]
        channel = plant.io_mapping["digital_outputs"][equipment]
        print(f"WRITES OUTPUTS: {transfer.pump_command} = {equipment} ({channel}) for "
              f"{transfer.pump_runtime_s:g} s per run; every other output is held off.")
        print(f"The pump runs only while {transfer.source_low_level} "
              f"({transfer.source_low_switch}) is reached.")
        print(f"Planned: {transfer.planned_runs} runs at the same runtime.")
    else:
        print("Manual measurement window: no output is written.")
    if simulate:
        print("SIMULATION: no hardware, the factor cannot be saved.")
    if not ask_yes("Continue? [y/N]: "):
        return

    io_class = SimulatedIO if simulate else HardwareIO
    io = io_class(plant, sensor, write_outputs=transfer is not None)
    sampler = Sampler(io, settings.sample_interval_s,
                      pump_command=transfer.pump_command if transfer else None,
                      level_switch=transfer.source_low_switch if transfer else None,
                      level_settings=plant.levels[transfer.source_low_switch] if transfer else None)
    runs = []
    try:
        sampler.start()
        if not sampler.ready.wait(5.0):
            raise RuntimeError("No hardware sample within 5 s")
        time.sleep(0.5)                                 # level debounce settles
        check_fault(sampler)
        slot = 1
        while True:
            if transfer:
                if slot > transfer.planned_runs:
                    if not ask_yes("\nPlanned runs done. Another run with the same runtime? [y/N]: "):
                        break
                label = f"Run {slot}/{transfer.planned_runs}"
                answer = ask(f"\n{label}: empty bucket in place at the outlet? "
                             "Enter to start, 'q' to finish: ").lower()
                if answer == "q":
                    break
                if sampler.status()["level"] is not True:
                    print(f"  Not started: {transfer.source_low_level} is not reached.")
                    continue
            else:
                print(f"\nRun {len(runs) + 1}: empty bucket in place.")
            run_number = len(runs) + 1
            try:
                if transfer:
                    result, timeout = transfer_window(sampler, transfer, settings)
                else:
                    result, timeout = manual_window(sampler)
            except _Aborted as aborted:
                print("\n  Run aborted, pump off. The record is kept as 'aborted'.")
                record = build_record(sensor, run_number, aborted.result, None, None,
                                      "aborted", settings)
                set_status(record, "aborted", "aborted by operator")
                runs.append(record)
                journal.write("run", record)
                check_fault(sampler)
                if not ask_yes("  Continue with another run? [Y/n]: ", default=True):
                    break
                continue
            volume, mass, note = enter_measurement(result)
            record = build_record(sensor, run_number, result, volume, mass, note, settings)
            if timeout:
                record["flags"].append("RUNDOWN_TIMEOUT")
            runs.append(record)
            journal.write("run", record)
            status = decide(record, runs, settings, journal)
            check_fault(sampler)
            if transfer:
                if status != "repeated":
                    slot += 1
            elif not ask_yes("\nConduct another measurement? [Y/n]: ", default=True):
                break
    finally:
        try:
            sampler.close()
        finally:
            io.close()
            if transfer:
                print("All outputs off.")

    if not runs:
        print("No runs recorded; nothing exported.")
        journal.close()
        return

    summary = review(runs, settings, journal)
    journal.close()
    export_dir = ROOT / "exports" / "flow_calibration"
    finished = utc_now()
    stem = f"{tag}{sensor}_calibration_{stamp(finished)}"
    csv_path = exclusive_path(export_dir, stem, ".csv")
    info = {
        "sensor_id": sensor,
        "session_start_utc": iso(session_start),
        "export_utc": iso(finished),
        "data_source": "SIMULATED" if simulate else "LIVE",
        "software_commit": git_commit(),
        "calibration_settings_pump_runtime_s": transfer.pump_runtime_s if transfer else None,
        "deviation_warning_percent": settings.deviation_warning_percent,
        "previous_pulses_per_litre": old.pulses_per_litre,
        "previous_calibration_confirmed": old.calibration_confirmed,
        "reference": "measured volume; density only as plausibility check",
        "formula": "K = sum(pulses)/sum(volume), accepted runs, no offset; "
                   "volume = pulses/K; flow L/min = pulses/K*60/interval_s",
    }
    summary_out = dict(summary)
    summary_out["inconsistent_runs"] = ";".join(map(str, summary["inconsistent_runs"]))
    write_export(csv_path, runs, summary_out, info)
    svg_path = csv_path.with_suffix(".svg")
    write_plot(svg_path, sensor, runs, summary, settings.deviation_warning_percent)
    print(f"\nExported: {csv_path.relative_to(ROOT)}\n          {svg_path.relative_to(ROOT)}")

    k = summary["k_combined"]
    if k is None:
        print("No accepted runs: no factor proposed, flow_settings.json unchanged.")
        return
    print(f"\nOld factor: {old.pulses_per_litre}   Proposed: {k:.6g} pulses/L"
          + ("" if old.pulses_per_litre is None
             else f"   change {(k - old.pulses_per_litre) / old.pulses_per_litre * 100:+.2f} %"))
    if summary["accepted_runs"] < 3:
        print("WARNING: fewer than 3 accepted runs.")
    if summary["inconsistent_runs"]:
        print(f"WARNING: inconsistent runs {summary['inconsistent_runs']} "
              f"(> {settings.deviation_warning_percent:g} % from the proposed K).")
    if simulate:
        print("Simulation: flow_settings.json is not changed.")
        return
    answer = ask(f"Type {sensor} to save {k:.6g} as pulses_per_litre "
                 "(calibration_confirmed is set to false), or Enter to keep the old value: ")
    if answer != sensor:
        print("Not saved. flow_settings.json unchanged.")
        return
    new_k, previous, backup = save_factor(config_dir, sensor, k, csv_path, summary)
    print(f"Saved {sensor}.pulses_per_litre = {new_k} (was {previous['pulses_per_litre']}); "
          f"calibration_confirmed = false.\nBackup: config/{backup.name}; "
          "history: config/flow_calibration_history.json.\n"
          "Set calibration_confirmed to true by hand once the factor is accepted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sensor", help="flow_sensor_1, flow_sensor_2 or flow_sensor_3")
    parser.add_argument("--simulate", action="store_true",
                        help="rehearse without hardware; nothing is written or saved")
    parser.add_argument("--settings", type=Path,
                        default=ROOT / "config" / "flow_calibration_settings.json")
    parser.add_argument("--lock", type=Path, default=ROOT / "logs" / "controller.lock")
    args = parser.parse_args()

    config_dir = ROOT / "config"
    plant = load_config(config_dir)
    settings = load_calibration_settings(args.settings, plant)
    sensor = args.sensor or choose_sensor(settings)
    if sensor not in settings.sensors:
        raise SystemExit(f"Unknown sensor {sensor!r}; expected one of {settings.sensors}")

    lock = None
    if not args.simulate:
        lock = RunLock(args.lock)
        if not lock.acquire():
            raise SystemExit("The controller is running (lock held). Stop it first; "
                             "nothing was written.")
    try:
        run_session(sensor, plant, settings, args.simulate, config_dir)
    except KeyboardInterrupt:
        print("\nStopped. Runs so far are in logs/flow_calibration/ (journal).")
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
