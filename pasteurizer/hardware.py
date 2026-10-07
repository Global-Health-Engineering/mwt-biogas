"""Read one shared snapshot of the configured hardware."""

import json
import time
from pathlib import Path

import revpimodio2


class HardwareReader:
    def __init__(self, mapping_path=None):
        if mapping_path is None:
            mapping_path = (
                Path(__file__).resolve().parents[1]
                / "config"
                / "io_mapping.json"
            )

        with open(mapping_path) as file:
            self.mapping = json.load(file)

        self.rpi = revpimodio2.RevPiModIO(
            autorefresh=False,
            monitoring=True,
        )

        try:
            # Resolve names at startup so mapping errors fail immediately.
            names = set(self.mapping["digital_inputs"].values())
            names.update(self.mapping["flow_counters"].values())

            for temperature in self.mapping["temperatures"].values():
                names.add(temperature["value"])
                names.add(temperature["status"])

            names.update(self.mapping["control_valve_Y4"].values())
            names.update(("Status", "Status_i06"))

            self.channels = {
                name: self.rpi.io[name]
                for name in names
            }
        except Exception:
            self.rpi.cleanup()
            raise

    def read(self):
        if not self.rpi.readprocimg():
            raise RuntimeError("Hardware read failed; snapshot unavailable")

        sampled_at = time.monotonic()

        def value(name):
            return self.channels[name].value

        return {
            "sampled_at": sampled_at,
            "module_status": {
                "dio": int(value("Status")),
                "di": int(value("Status_i06")),
            },
            "digital_inputs": {
                name: bool(value(channel))
                for name, channel
                in self.mapping["digital_inputs"].items()
            },
            "flow_counters": {
                name: int(value(channel))
                for name, channel
                in self.mapping["flow_counters"].items()
            },
            "temperatures": {
                name: {
                    "raw": int(value(channels["value"])),
                    "status": int(value(channels["status"])),
                }
                for name, channels
                in self.mapping["temperatures"].items()
            },
            "control_valve": {
                name: int(value(channel))
                for name, channel
                in self.mapping["control_valve_Y4"].items()
            },
        }

    def close(self):
        self.rpi.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()