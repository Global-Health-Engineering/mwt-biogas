"""Read-only monitor for the six mapped temperature sensors."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import revpimodio2


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from pasteurizer.temperatures import process_temperature


def main():
    mapping_path = PROJECT_ROOT / "config" / "io_mapping.json"

    with mapping_path.open() as file:
        mapping = json.load(file)

    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    try:
        # Resolve all configured names before starting.
        channels = {
            name: (
                rpi.io[definition["value"]],
                rpi.io[definition["status"]],
            )
            for name, definition in mapping["temperatures"].items()
        }

        print("Read-only temperature monitor. Ctrl+C to stop.")

        while True:
            if not rpi.readprocimg():
                raise RuntimeError(
                    "Process-image read failed; temperatures unavailable"
                )

            print(f"\n{datetime.now():%H:%M:%S}")

            for name, (value_io, status_io) in channels.items():
                reading = process_temperature(
                    value_io.value,
                    status_io.value,
                )

                if reading.valid:
                    message = f"{reading.celsius:.1f} °C"
                else:
                    message = f"INVALID — {reading.error}"

                print(
                    f"{name:<10} {message}"
                    f"  [raw={reading.raw}, status={reading.status}]",
                    flush=True,
                )

            time.sleep(2)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        rpi.cleanup()


if __name__ == "__main__":
    main()