"""Run the controller against the plant simulator in real time, for the GUI.

    python tools/run_simulation.py                       # 1x speed, publisher on 8081
    python tools/run_simulation.py --speed 5             # 5x simulated time
    python tools/run_simulation.py --fault overtemperature@120+30 --fault stall@200+5

Hardware outputs are never written: the only output writer is the simulator.
All plant numbers are PRETEND values (flow factors, bath, heat capacity).
Records go to logs/simulation.sqlite with source SIMULATED. Stop with Ctrl+C
(orderly shutdown: safe frame, state saved, historian flushed).

Faults (NAME@START_S[+DURATION_S], simulated seconds):
  write_failure, read_failure, pt3_invalid, y4_feedback_invalid,
  transfer1_no_flow, overtemperature, historian_down,
  stall (the controller stops cycling: tests disconnect and watchdog)
"""

import argparse
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from pasteurizer.clock import FakeClock, MonotonicClock  # noqa: E402
from pasteurizer.clock_sync import KernelClockSync  # noqa: E402
from pasteurizer.command_server import CommandServer  # noqa: E402
from pasteurizer.controller import PlantController  # noqa: E402
from pasteurizer.event_log import EventLog  # noqa: E402
from pasteurizer.historian import Historian  # noqa: E402
from pasteurizer.modes import Mode  # noqa: E402
from pasteurizer.persistence import FileStore  # noqa: E402
from pasteurizer.publisher import StatusPublisher  # noqa: E402
from pasteurizer.simulated_io import PlantParameters, SimulatedPlant  # noqa: E402
from pasteurizer.watchdog import SoftwareWatchdog  # noqa: E402
from scenarios.harness import FAST, TestSqliteStore, make_config  # noqa: E402

CYCLE_S = 0.2


class SteppedClock(FakeClock):
    """Simulated time: exactly 0.2 s per scan, paced at `speed` x real time."""

    def __init__(self):
        super().__init__(1000.0)
        self._wall0 = time.time()

    def wall(self):
        return self._wall0 + (self.monotonic() - 1000.0)


def apply_fault(name, plant, store, on):
    f = plant.faults
    if name == "write_failure":
        f.write_failure = on
    elif name == "read_failure":
        f.read_failure = on
    elif name == "pt3_invalid":
        (f.invalid_temperatures.add if on else f.invalid_temperatures.discard)("PT3_B4")
    elif name == "y4_feedback_invalid":
        f.y4_feedback_invalid = on
    elif name == "transfer1_no_flow":
        (f.no_pulses.add if on else f.no_pulses.discard)("flow_sensor_1")
    elif name == "overtemperature":
        f.temperature_offsets["PT6_B7"] = 8.0 if on else 0.0
    elif name == "historian_down":
        store.fail = on
    elif name != "stall":
        raise ValueError(f"Unknown fault {name}")


def parse_fault(text):
    name, _, timing = text.partition("@")
    start, _, duration = timing.partition("+")
    return name, float(start or 0), float(duration) if duration else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--bath", type=float, default=40.0, help="initial PT5 in C")
    parser.add_argument("--tank1", type=float, default=900.0, help="tank 1 litres (PRETEND)")
    parser.add_argument("--database", type=Path, default=ROOT / "logs" / "simulation.sqlite")
    parser.add_argument("--state", type=Path, default=ROOT / "logs" / "simulation_state.json")
    parser.add_argument("--port", type=int, default=8081, help="status publisher port")
    parser.add_argument("--fault", action="append", default=[], type=parse_fault)
    parser.add_argument("--minutes", type=float, default=None, help="stop after (real)")
    parser.add_argument("--selector", choices=("AUTO", "MANUAL", "OFF"), default="AUTO",
                        help="simulated selector S1 position")
    parser.add_argument("--token", type=Path, default=ROOT / "logs" / "command.token")
    parser.add_argument("--command-port", type=int, default=8082)
    args = parser.parse_args()
    if not 0.1 <= args.speed <= 20:
        parser.error("--speed must be between 0.1 and 20")
    # Speed 1: real clocks. Other speeds: simulated time in exact 0.2 s steps
    # (timestamps are then marked SIMULATED in the historian and the GUI).

    config = make_config()
    stepped = args.speed != 1.0
    if stepped:
        clock = SteppedClock()
        wall = clock.wall
    else:
        clock, wall = MonotonicClock(), time.time
    plant = SimulatedPlant(config, clock, PlantParameters(**FAST))
    plant.bath_c = args.bath
    plant.mode = Mode[args.selector]
    plant.tank[1] = args.tank1
    store = TestSqliteStore(args.database, config.control.logging)
    historian = Historian(store, config.control.logging, wall_clock=wall,
                          clock_sync=KernelClockSync())
    historian.start()
    log = EventLog(historian, clock=clock, wall_clock=wall, clock_sync=KernelClockSync())
    publisher = StatusPublisher(port=args.port, config=config)
    if not publisher.start():
        print(f"Status publisher not started: {publisher.error_category} on port {args.port} "
              f"(code {publisher.error_code}: {publisher.error}); controller runs anyway")

    commands = CommandServer(token_path=args.token, port=args.command_port)
    if not commands.start():
        print(f"Command server not started: {commands.error_category} ({commands.error}); "
              "GUI commands unavailable")

    def watchdog_trip():
        plant.watchdog_shutdown()
        print("WATCHDOG: no heartbeat, simulator outputs shut down")

    watchdog = SoftwareWatchdog(config.control.watchdog.heartbeat_timeout_s, watchdog_trip)
    ctl = PlantController(config, reader=plant, writer=plant, store=FileStore(args.state),
                          event_log=log, clock=clock, simulated_io=True,
                          heartbeat=watchdog.heartbeat, publish=publisher.publish)
    stop = threading.Event()

    def watch():
        while not stop.wait(0.1):
            watchdog.check(clock.monotonic())
    threading.Thread(target=watch, name="watchdog", daemon=True).start()

    print(f"Simulation running (speed {args.speed}x). Status: http://127.0.0.1:{publisher.port}"
          f"/status  History: {args.database}. Ctrl+C stops.")
    t0, real0 = clock.monotonic(), time.monotonic()
    active = {}
    try:
        while args.minutes is None or time.monotonic() - real0 < args.minutes * 60:
            started = time.monotonic()
            if stepped:
                clock.advance(CYCLE_S)
            elapsed = clock.monotonic() - t0
            for name, start, duration in args.fault:
                on = elapsed >= start and (duration is None or elapsed < start + duration)
                if active.get((name, start)) != on:
                    active[(name, start)] = on
                    apply_fault(name, plant, store, on)
                    print(f"{elapsed:7.1f}s fault {name} {'ON' if on else 'OFF'}")
            stalled = any(on for (name, _), on in active.items() if name == "stall")
            if not stalled:
                commands.execute_pending(ctl)
                status = ctl.cycle()
                if ctl.cycle_count % 25 == 0:
                    print(f"{elapsed:7.1f}s {status.state:18s} {status.state_reason}")
            # 0.2 s of simulated time per scan, whatever the speed.
            time.sleep(max(0.0, CYCLE_S / args.speed - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        commands.stop()
        result = ctl.shutdown("simulation stopped")
        publisher.stop()
        print(f"Stopped: safe frame written {result.safe_frame_written}, "
              f"{result.records_not_stored} records not stored")


if __name__ == "__main__":
    main()
