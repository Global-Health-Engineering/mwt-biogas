import json
import sys
import time
from datetime import datetime
from pathlib import Path

import revpimodio2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.flows import FlowProcessor


def main():
    with open(ROOT / "config/io_mapping.json") as file:
        mapping = json.load(file)

    with open(ROOT / "config/flow_settings.json") as file:
        settings = json.load(file)

    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    try:
        channels = {
            name: rpi.io[mapping["flow_counters"][name]]
            for name in settings
        }
        processors = {
            name: FlowProcessor(**options)
            for name, options in settings.items()
        }

        print("Read-only flow monitor. Ctrl+C to stop.")

        while True:
            if not rpi.readprocimg():
                raise RuntimeError("Read failed; flow states unavailable")

            # Hall sensors belong to the DIO: check its input fault bits.
            status = int(rpi.io["Status"].value)
            if status & 0x00FF:
                raise RuntimeError(
                    f"DIO input fault: status=0x{status:04X}"
                )

            now = time.monotonic()
            timestamp = datetime.now().strftime("%H:%M:%S")

            for name, channel in channels.items():
                reading = processors[name].update(channel.value, now)

                if not reading.valid:
                    result = f"UNAVAILABLE: {reading.error}"
                elif reading.flow_lpm is None:
                    result = (
                        f"{reading.pulse_rate_hz:.2f} pulses/s"
                        " | L/min unavailable: conversion factor missing"
                    )
                else:
                    label = "CALIBRATED" if reading.calibrated else "ESTIMATE"
                    result = (
                        f"{reading.flow_lpm:.2f} L/min"
                        f" | measured={reading.measured_volume_litres:.3f} L"
                        f" | {label}"
                    )

                print(
                    f"{timestamp}  {name}"
                    f"  counter={reading.raw_counter}  {result}",
                    flush=True,
                )

            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        rpi.cleanup()


if __name__ == "__main__":
    main()