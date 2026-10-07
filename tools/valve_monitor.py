import json
import sys
import time
from datetime import datetime
from pathlib import Path

import revpimodio2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.valves import process_feedback


def main():
    with open(ROOT / "config/io_mapping.json") as file:
        mapping = json.load(file)["control_valve_Y4"]

    with open(ROOT / "config/valve_settings.json") as file:
        settings = json.load(file)

    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    try:
        value = rpi.io[mapping["feedback"]]
        status = rpi.io[mapping["feedback_status"]]

        print("Read-only valve feedback monitor. Ctrl+C to stop.")

        while True:
            if not rpi.readprocimg():
                raise RuntimeError("Read failed; valve feedback unavailable")

            feedback = process_feedback(
                value.value,
                status.value,
                settings["feedback_closed_raw"],
                settings["feedback_open_raw"],
                settings["feedback_margin_percent"],
            )

            if feedback.valid:
                result = f"position={feedback.position_percent:.1f}%"
            else:
                result = f"UNAVAILABLE: {feedback.error}"

            print(
                f"{datetime.now():%H:%M:%S}"
                f"  raw={feedback.raw}"
                f"  status={feedback.status}"
                f"  {result}",
                flush=True,
            )
            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        rpi.cleanup()


if __name__ == "__main__":
    main()