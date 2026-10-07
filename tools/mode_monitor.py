"""Read-only monitor for selector contacts and requested mode."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import revpimodio2


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from pasteurizer.modes import process_mode


def main():
    with (PROJECT_ROOT / "config" / "io_mapping.json").open() as file:
        mapping = json.load(file)

    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    try:
        inputs = mapping["digital_inputs"]
        manual_io = rpi.io[inputs["manual_switch"]]
        auto_io = rpi.io[inputs["auto_switch"]]
        previous = None

        print("Read-only selector monitor. Ctrl+C to stop.")

        while True:
            if not rpi.readprocimg():
                raise RuntimeError(
                    "Process-image read failed; selector mode unavailable"
                )

            manual = bool(manual_io.value)
            auto = bool(auto_io.value)
            mode = process_mode(manual, auto)
            current = (manual, auto, mode)

            if current != previous:
                print(
                    f"{datetime.now():%H:%M:%S}  "
                    f"MANUAL contact={manual}  "
                    f"AUTO contact={auto}  "
                    f"MODE={mode.value}",
                    flush=True,
                )
                previous = current

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        rpi.cleanup()


if __name__ == "__main__":
    main()