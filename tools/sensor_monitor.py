"""Read-only monitoring and logging using the shared signal processor."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.hardware import HardwareReader
from pasteurizer.signals import SignalProcessor


def main():
    processor = SignalProcessor()

    log_directory = ROOT / "logs"
    log_directory.mkdir(exist_ok=True)

    filename = datetime.now(timezone.utc).strftime(
        "signals_%Y%m%dT%H%M%S_%fZ.jsonl"
    )
    log_path = log_directory / filename

    print("Read-only combined signal monitor. Ctrl+C to stop.")
    print(f"Log: {log_path}", flush=True)

    try:
        with HardwareReader() as hardware, log_path.open("x") as logfile:
            next_report = time.monotonic()
            next_sample = next_report

            while True:
                snapshot = hardware.read()
                signals = processor.process(snapshot)
                now = time.monotonic()

                if now >= next_report:
                    record = {
                        "timestamp_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "snapshot": snapshot,
                        "signals": signals,
                    }

                    logfile.write(
                        json.dumps(record, allow_nan=False) + "\n"
                    )
                    logfile.flush()

                    print(
                        json.dumps(record, indent=2, allow_nan=False),
                        flush=True,
                    )
                    next_report = now + 1.0

                next_sample += 0.2
                now = time.monotonic()

                if next_sample <= now:
                    next_sample = now + 0.2

                time.sleep(max(0.0, next_sample - now))

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        processor.reset()
        print(f"Log path: {log_path}")


if __name__ == "__main__":
    main()