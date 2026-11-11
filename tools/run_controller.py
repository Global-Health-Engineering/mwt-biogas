"""The plant controller on the RevPi: live inputs, outputs written in every mode.

    python3 tools/run_controller.py                     # production: outputs enabled
    python3 tools/run_controller.py --outputs disabled  # diagnostic: nothing written
    python3 tools/run_gui.py --database logs/controller.sqlite   # (disabled: logs/shadow.sqlite)

On the RevPi the controller runs as the systemd service
deploy/mwt-biogas-controller.service (see that file and the README).

Outputs enabled (default):
  * AUTO runs the plant by the agreed control logic; OFF and MANUAL as in the
    specification. Every frame passes the arbiter and is written whole.
  * MANUAL: COMMISSIONING users switch equipment from the GUI. A request
    expires 30 s after it was last renewed, interlocks stay active, every
    command is logged. Requests and overrides are cleared on logout, session
    expiry, leaving MANUAL and controller restart (they live in memory only).

--outputs disabled (diagnostic): same logic and commands, nothing is ever
written; status and log say NOT_WRITTEN_OUTPUTS_DISABLED.

Start and stop:
  * At start the writer writes all outputs off before the first scan.
  * SIGINT (Ctrl+C, systemctl stop) or SIGTERM: the scan finishes its cycle,
    then the orderly shutdown runs: safe frame, state saved, CONTROLLER_STOP,
    historian flushed, then all outputs off. Exit code 0.
  * An unhandled error: the same shutdown, the abnormal stop is recorded in
    the state file (next start latches WATCHDOG_RESTART), exit code 1.
  * No heartbeat for heartbeat_timeout_s (scan hung or failing writes): the
    watchdog thread latches all outputs off, records the abnormal stop and the
    runner stops (exit code 3). If the scan stays stuck, the process ends
    itself EXIT_GRACE_S later.
  * Killed (SIGKILL) or hung as a whole: the outputs keep their last value
    until systemd's ExecStopPost (tools/safe_outputs.py) switches them off.
  * Under systemd: READY=1 after the first scan, WATCHDOG=1 after every scan
    with a successful write, STOPPING=1 at shutdown.

Files (separate per output mode, never shared with the simulation):
    enabled:   logs/controller.sqlite, logs/controller_state.json
    disabled:  logs/shadow.sqlite,     logs/shadow_state.json
    both:      logs/controller.lock (one controller on the hardware),
               logs/command.token (GUI -> controller commands; removed at stop)

Reset of latched faults: RESET in the GUI (logged in). To clear the state
file instead: stop the controller, check `pgrep -af run_controller.py` prints
nothing, delete the state file of that mode, start again. Never delete it
while the controller is running.
"""

import argparse
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
from pasteurizer.command_server import CommandServer  # noqa: E402
from pasteurizer.config import MODE_CONTACTS, load_config  # noqa: E402
from pasteurizer.controller import PlantController  # noqa: E402
from pasteurizer.event_log import EventLog  # noqa: E402
from pasteurizer.historian import Historian, SqliteStore  # noqa: E402
from pasteurizer.persistence import FileStore, mark_abnormal_stop  # noqa: E402
from pasteurizer.publisher import StatusPublisher  # noqa: E402
from pasteurizer.run_lock import RunLock  # noqa: E402
from pasteurizer.shadow_outputs import ShadowOutputWriter  # noqa: E402
from pasteurizer.systemd_notify import SystemdNotifier  # noqa: E402
from pasteurizer.watchdog import SoftwareWatchdog  # noqa: E402

CYCLE_S = 0.2
EXIT_GRACE_S = 10.0         # a scan still stuck this long after a watchdog trip ends the process
LOGS = ROOT / "logs"
FILES = {
    "enabled": ("controller.sqlite", "controller_state.json"),
    "disabled": ("shadow.sqlite", "shadow_state.json"),
}
OTHER_RUNS = {"simulation.sqlite", "simulation_state.json", "historian.sqlite"}
EXIT_OK, EXIT_ERROR, EXIT_WATCHDOG = 0, 1, 3


def reader_mapping(config) -> dict:
    """The I/O mapping with only the digital inputs the controller uses.

    Disabled level switches (level_switch_10) are not resolved or read, so an
    unused input can neither stop the start nor appear anywhere.
    """
    import copy
    used = set(MODE_CONTACTS) | {name for name, options in config.levels.items()
                                 if options.enabled}
    mapping = copy.deepcopy(config.io_mapping)
    mapping["digital_inputs"] = {name: channel
                                 for name, channel in mapping["digital_inputs"].items()
                                 if name in used}
    return mapping


def guarded(problems, name, action):
    """Run one shutdown step; a failing step never skips the later ones."""
    try:
        return action()
    except BaseException as error:          # noqa: BLE001 - shutdown must continue
        problems.append(f"{name}: {type(error).__name__}: {error}")
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--outputs", choices=tuple(FILES), default="enabled")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--lock", type=Path, default=LOGS / "controller.lock")
    parser.add_argument("--token", type=Path, default=LOGS / "command.token")
    parser.add_argument("--port", type=int, default=8081, help="status publisher port")
    parser.add_argument("--command-port", type=int, default=8082)
    parser.add_argument("--minutes", type=float, default=None, help="stop after (tests)")
    args = parser.parse_args()
    database_name, state_name = FILES[args.outputs]
    args.database = args.database or LOGS / database_name
    args.state = args.state or LOGS / state_name
    forbidden = OTHER_RUNS | {name for mode, names in FILES.items()
                              if mode != args.outputs for name in names}
    for path in (args.database, args.state):
        if path.name in forbidden:
            parser.error(f"{path} belongs to another run; files must be separate")

    lock = RunLock(args.lock)
    if not lock.acquire():
        raise SystemExit(f"Another controller is running (lock {args.lock}). Stop it first.")

    from pasteurizer.hardware import HardwareReader     # needs revpimodio2 (RevPi)

    enabled = args.outputs == "enabled"
    config = load_config()
    clock = MonotonicClock()
    notifier = SystemdNotifier()
    reader = HardwareReader(mapping=reader_mapping(config), clock=clock)
    if enabled:
        from pasteurizer.hardware_outputs import HardwareOutputWriter
        writer = HardwareOutputWriter(config)            # writes all off at once
    else:
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
    commands = CommandServer(token_path=args.token, port=args.command_port)
    if not commands.start():
        print(f"Command server not started: {commands.error_category} "
              f"({commands.error}); GUI commands unavailable")
    stop_requested = threading.Event()
    shutting_down = threading.Event()   # the orderly shutdown has begun (no heartbeats)
    stopped = threading.Event()         # set when the main thread has finished its shutdown
    tripped = threading.Event()

    def watchdog_trip():
        # The scan stopped: latch the outputs off from this thread, record the
        # abnormal stop (next start latches WATCHDOG_RESTART) and end the run.
        written = writer.inhibit() if enabled else False
        marked = mark_abnormal_stop(args.state)
        tripped.set()
        print(f"WATCHDOG: no heartbeat; outputs latched off (all-off written now: {written}), "
              f"abnormal stop {marked}; controller stopping", flush=True)

        def force_exit():
            if not stopped.wait(EXIT_GRACE_S):
                print("WATCHDOG: scan still stuck; ending the process", flush=True)
                os._exit(EXIT_WATCHDOG)
        threading.Thread(target=force_exit, name="watchdog-exit", daemon=True).start()

    watchdog = SoftwareWatchdog(config.control.watchdog.heartbeat_timeout_s, watchdog_trip)
    ctl = PlantController(config, reader=reader, writer=writer, store=state_store,
                          event_log=log, clock=clock, simulated_io=False,
                          heartbeat=watchdog.heartbeat, publish=publisher.publish)
    if ctl.outputs_enabled is not enabled:
        raise SystemExit("Refused: output writer does not match --outputs")

    def watch():
        # Supervises the scan until the orderly shutdown begins; a shutdown
        # that hangs is ended by systemd (TimeoutStopSec, then ExecStopPost).
        while not shutting_down.wait(0.1):
            watchdog.check(clock.monotonic())
    threading.Thread(target=watch, name="watchdog", daemon=True).start()

    def request_stop(signum, _frame):
        # First signal: finish the current scan, then shut down in order.
        # Second signal: interrupt whatever is running (shutdown still runs).
        if stop_requested.is_set():
            raise KeyboardInterrupt
        stop_requested.set()
        print(f"Stop requested ({signal.Signals(signum).name})", flush=True)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print("LIVE INPUTS - OUTPUTS ENABLED: the controller writes the RevPi outputs in every mode."
          if enabled else
          "LIVE INPUTS - OUTPUTS DISABLED / SHADOW RUN (diagnostic). Nothing is written to RevPi outputs.")
    print(f"Status: http://127.0.0.1:{publisher.port}/status  Commands: 127.0.0.1:{commands.port}  "
          f"History: {args.database}  State: {args.state}. Ctrl+C stops.", flush=True)
    real0 = time.monotonic()
    failure = None
    ready_sent = False
    try:
        while not (tripped.is_set() or stop_requested.is_set()) and (
                args.minutes is None or time.monotonic() - real0 < args.minutes * 60):
            started = time.monotonic()
            commands.execute_pending(ctl)
            status = ctl.cycle()
            if ctl.last_write_ok:           # heartbeat only after a good scan and write
                if not ready_sent:
                    ready_sent = notifier.ready(f"{status.mode} {status.state}") or True
                notifier.watchdog()
            if ctl.cycle_count % 25 == 0:
                active = sum(1 for a in status.alarms if a.active)
                print(f"{time.strftime('%H:%M:%S')} mode {status.mode:7s} {status.state:18s} "
                      f"{status.state_reason} | alarms {len(status.alarms)} ({active} active) "
                      f"| outputs {status.output_write}", flush=True)
            time.sleep(max(0.0, CYCLE_S - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    except Exception as error:          # recorded, then the same orderly shutdown
        failure = error
        print(f"CONTROLLER ERROR: {type(error).__name__}: {error}", flush=True)
    finally:
        shutting_down.set()
        problems = []
        notifier.stopping("shutting down")
        guarded(problems, "command server", commands.stop)
        reason = ("watchdog trip" if tripped.is_set() else
                  f"error: {type(failure).__name__}" if failure else "controller stopped")
        result = guarded(problems, "controller shutdown", lambda: ctl.shutdown(reason))
        if enabled:
            guarded(problems, "all outputs off", writer.close)   # lamps included
        if tripped.is_set() or failure is not None:
            marked = guarded(problems, "abnormal stop marker", lambda: mark_abnormal_stop(args.state))
            print(f"Abnormal stop recorded in {args.state}: {marked}")
        guarded(problems, "status publisher", publisher.stop)
        guarded(problems, "input reader", reader.close)
        guarded(problems, "lock", lock.release)
        stopped.set()
        if result is not None:
            print("Controller stopped: " + (
                f"safe frame written {result.safe_frame_written}, then all outputs off"
                if enabled else "safe frame NOT written (outputs disabled)")
                + f", state saved {result.state_saved}, {result.records_not_stored} records not stored")
        for problem in problems:
            print(f"SHUTDOWN STEP FAILED - {problem}")
        notifier.close()
    if failure is not None:
        raise failure                   # traceback; exit code 1
    if tripped.is_set():
        sys.exit(EXIT_WATCHDOG)


if __name__ == "__main__":
    main()
