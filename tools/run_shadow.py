"""Shadow run on the RevPi: live inputs, outputs disabled.

    python3 tools/run_shadow.py                  # status publisher on 127.0.0.1:8081
    python3 tools/run_gui.py --database logs/shadow.sqlite

The controller reads the real inputs (HardwareReader, revpimodio2 in
monitoring mode, which cannot write outputs) and runs its normal logic and
supervision. Every command goes to ShadowOutputWriter, which keeps it in
memory only: nothing is ever written to a RevPi output. The GUI shows
"LIVE INPUTS - OUTPUTS DISABLED / SHADOW RUN", the log records
OUTPUT_NOT_WRITTEN, and the shutdown reports the safe frame as not written.

Because nothing moves, checks that wait for an actuator response raise their
faults (transfer no-flow, preheat timeout, Y4 position timeout). Flow
calibration is pending: pulse counts are shown, L/min is unavailable.

Files (separate from simulation and future live operation):
    logs/shadow.sqlite        history (events and samples, source LIVE)
    logs/shadow_state.json    controller state: latched faults, STOP hold
    logs/shadow.lock          held while this runner is running

Reset (clears latched faults and STOP hold of the shadow run only):
    1. stop this runner with Ctrl+C and wait for "Shadow run stopped"
    2. check it is gone:   pgrep -af run_shadow.py   (prints nothing)
    3. rm logs/shadow_state.json
    4. start it again
Never delete the state file while the runner is running: it saves the state
again on every change and at shutdown, so the deletion would be undone or
leave a state that does not match the log. The history file is kept.
"""

import argparse
import copy
import os
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.clock import MonotonicClock  # noqa: E402
from pasteurizer.clock_sync import KernelClockSync  # noqa: E402
from pasteurizer.config import MODE_CONTACTS, load_config  # noqa: E402
from pasteurizer.controller import PlantController  # noqa: E402
from pasteurizer.event_log import EventLog  # noqa: E402
from pasteurizer.historian import Historian, SqliteStore  # noqa: E402
from pasteurizer.persistence import FileStore  # noqa: E402
from pasteurizer.publisher import StatusPublisher  # noqa: E402
from pasteurizer.shadow_outputs import ShadowOutputWriter  # noqa: E402
from pasteurizer.watchdog import SoftwareWatchdog  # noqa: E402

CYCLE_S = 0.2
LOGS = ROOT / "logs"
# Files of other runs that a shadow run must never write to.
RESERVED = {"simulation.sqlite", "simulation_state.json", "historian.sqlite"}


def reader_mapping(config) -> dict:
    """The I/O mapping with only the digital inputs the controller uses.

    Disabled level switches (level_switch_10) are not resolved or read, so an
    unused input can neither stop the start nor appear anywhere.
    """
    used = set(MODE_CONTACTS) | {name for name, options in config.levels.items()
                                 if options.enabled}
    mapping = copy.deepcopy(config.io_mapping)
    mapping["digital_inputs"] = {name: channel
                                 for name, channel in mapping["digital_inputs"].items()
                                 if name in used}
    return mapping


class RunLock:
    """One shadow runner at a time (an OS lock, released even after a crash).

    Linux: flock on the whole file. Windows: msvcrt.locking locks bytes from
    the current file position, so the lock is always taken on byte 0 (never
    at the end of the file, where every runner would lock a different byte).
    """

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Read/write without append mode: seek(0) also applies to writes.
        self.file = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:                     # held by another runner
            self.file.close()
            self.file = None
            return False
        # Only the holder writes (its process id, for diagnosis).
        self.file.seek(0)
        self.file.truncate()
        self.file.write(f"{os.getpid()}\n")
        self.file.flush()
        return True

    def release(self):
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass                            # closing the file releases it anyway
        finally:
            self.file.close()
            self.file = None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, default=LOGS / "shadow.sqlite")
    parser.add_argument("--state", type=Path, default=LOGS / "shadow_state.json")
    parser.add_argument("--lock", type=Path, default=LOGS / "shadow.lock")
    parser.add_argument("--port", type=int, default=8081, help="status publisher port")
    parser.add_argument("--minutes", type=float, default=None, help="stop after (tests)")
    args = parser.parse_args()
    for path in (args.database, args.state):
        if path.name in RESERVED:
            parser.error(f"{path} belongs to another run; shadow files must be separate")

    lock = RunLock(args.lock)
    if not lock.acquire():
        raise SystemExit(f"Another shadow run is active (lock {args.lock}). Stop it first.")

    # Imported here: needs revpimodio2 on the RevPi. monitoring=True inside.
    from pasteurizer.hardware import HardwareReader

    config = load_config()
    clock = MonotonicClock()
    reader = HardwareReader(mapping=reader_mapping(config), clock=clock)
    writer = ShadowOutputWriter()
    store = SqliteStore(args.database, config.control.logging)
    historian = Historian(store, config.control.logging, wall_clock=time.time,
                          clock_sync=KernelClockSync())
    historian.start()
    log = EventLog(historian, clock=clock, wall_clock=time.time, clock_sync=KernelClockSync())
    state_store = FileStore(args.state)
    publisher = StatusPublisher(port=args.port, config=config)
    if not publisher.start():
        print(f"Status publisher not started: {publisher.error_category} on port {args.port} "
              f"(code {publisher.error_code}: {publisher.error}); controller runs anyway")

    def watchdog_trip():
        # No outputs to switch off. Mark the state so the next start latches
        # WATCHDOG_RESTART, exactly as a real run would.
        state = state_store.load()
        state.watchdog_tripped = True
        state_store.save(state)
        print("WATCHDOG: no heartbeat; WATCHDOG_RESTART will latch at the next start")

    watchdog = SoftwareWatchdog(config.control.watchdog.heartbeat_timeout_s, watchdog_trip)
    ctl = PlantController(config, reader=reader, writer=writer, store=state_store,
                          event_log=log, clock=clock, simulated_io=False,
                          heartbeat=watchdog.heartbeat, publish=publisher.publish)
    if ctl.outputs_enabled is not False:        # never run this file with a real writer
        raise SystemExit("Shadow run refused: the output writer has outputs enabled")
    stop = threading.Event()

    def watch():
        while not stop.wait(0.1):
            watchdog.check(clock.monotonic())
    threading.Thread(target=watch, name="watchdog", daemon=True).start()

    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)

    print("LIVE INPUTS - OUTPUTS DISABLED / SHADOW RUN. Nothing is written to RevPi outputs.")
    print(f"Status: http://127.0.0.1:{publisher.port}/status  History: {args.database}  "
          f"State: {args.state}. Ctrl+C stops.")
    real0 = time.monotonic()
    try:
        while args.minutes is None or time.monotonic() - real0 < args.minutes * 60:
            started = time.monotonic()
            status = ctl.cycle()
            if ctl.cycle_count % 25 == 0:
                active = sum(1 for a in status.alarms if a.active)
                print(f"{time.strftime('%H:%M:%S')} mode {status.mode:7s} {status.state:18s} "
                      f"{status.state_reason} | alarms {len(status.alarms)} "
                      f"({active} active) | outputs NOT written")
            time.sleep(max(0.0, CYCLE_S - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        result = ctl.shutdown("shadow run stopped")
        publisher.stop()
        reader.close()
        lock.release()
        print(f"Shadow run stopped: safe frame NOT written (outputs disabled), "
              f"state saved {result.state_saved}, "
              f"{result.records_not_stored} records not stored")


if __name__ == "__main__":
    main()
