"""Read-only monitor for enabled float switches."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import revpimodio2


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from pasteurizer.levels import LevelProcessor


def main():
    with (PROJECT_ROOT / "config/io_mapping.json").open() as file:
        mapping = json.load(file)

    with (PROJECT_ROOT / "config/level_settings.json").open() as file:
        settings = json.load(file)

    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    processors = {}
    try:
        channels = {}

        for name, options in settings.items():
            if not options["enabled"]:
                continue

            channels[name] = rpi.io[mapping["digital_inputs"][name]]
            processors[name] = LevelProcessor(
                active_high=options["active_high"],
                debounce_seconds=options["debounce_seconds"],
            )

        if not channels:
            raise RuntimeError("No level switches are enabled")

        previous = {}
        last_read = None
        print("Read-only level monitor. Ctrl+C to stop.")

        while True:
            if not rpi.readprocimg():
                raise RuntimeError("Read failed; level states unavailable")
            
            di_status = int(rpi.io["Status_i06"].value)
            
            # DI input fault flags are bits 0–7.
            # Ignore output-related bits on this input-only module.
            if di_status & 0x00FF:
                raise RuntimeError(
                    f"DI input fault: status=0x{di_status:04X}; "
                    "level states unavailable"
                )

            now = time.monotonic()

            # Do not accept a transition across a long sampling interruption.
            if last_read is not None and now - last_read > 0.5:
                for processor in processors.values():
                    processor.reset()
                print("Sampling interrupted; rechecking level states.")

            last_read = now

            for name, channel in channels.items():
                reading = processors[name].update(channel.value, now)

                if reading != previous.get(name):
                    state = {
                        None: "UNKNOWN",
                        False: "BELOW SWITCH",
                        True: "LEVEL REACHED",
                    }[reading.reached]

                    print(
                        f"{datetime.now():%H:%M:%S}  {name}  "
                        f"raw={reading.raw}  {state}  "
                        f"pending={reading.pending}",
                        flush=True,
                    )
                    previous[name] = reading

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        for processor in processors.values():
            processor.reset()
        rpi.cleanup()


if __name__ == "__main__":
    main()