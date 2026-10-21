"""Read-only preliminary hydraulic monitor. Does not control outputs."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import revpimodio2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.flows import FlowProcessor
from pasteurizer.temperatures import process_temperature
from treatment_model.hydraulics import calculate_hydraulics


def load_config(name):
    with (ROOT / "config" / name).open() as file:
        return json.load(file)


def main():
    mapping = load_config("io_mapping.json")
    settings = load_config("treatment_settings.json")
    flow_settings = load_config("flow_settings.json")
    roles = load_config("equipment_roles.json")
    sensors = settings["sensors"]

    flow_name = sensors["effluent_flow"]
    processor = FlowProcessor(**flow_settings[flow_name])

    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    try:
        counter = rpi.io[mapping["flow_counters"][flow_name]]
        sensor_keys = (
            "flowmeter_temperature",
            "effluent_inlet_temperature",
            "effluent_outlet_temperature",
        )
        channels = {}
        for key in sensor_keys:
            definition = mapping["temperatures"][sensors[key]]
            channels[key] = (
                rpi.io[definition["value"]],
                rpi.io[definition["status"]],
            )

        print("Read-only preliminary hydraulics. Ctrl+C to stop.")
        print("E. coli reduction: UNAVAILABLE — thermal/kinetic models pending.")
        print(
            "Physical assignments verified:",
            roles.get("physical_assignments_verified", False),
        )
        print("Residence times assume steady flow and an equal branch temperature.")

        while True:
            if not rpi.readprocimg():
                raise RuntimeError("Process-image read failed")

            status = int(rpi.io["Status"].value)
            if status & 0x00FF:
                processor.reset()
                raise RuntimeError(f"DIO input fault: 0x{status:04X}")

            flow = processor.update(counter.value, time.monotonic())
            temperatures = {
                key: process_temperature(value.value, status_io.value)
                for key, (value, status_io) in channels.items()
            }

            print(f"\n{datetime.now():%H:%M:%S}", flush=True)

            try:
                if not flow.valid:
                    raise ValueError(flow.error)
                if flow.flow_lpm is None:
                    raise ValueError("Flow conversion factor missing")
                for key, reading in temperatures.items():
                    if not reading.valid:
                        raise ValueError(f"{sensors[key]}: {reading.error}")

                inlet = temperatures["effluent_inlet_temperature"].celsius
                outlet = temperatures["effluent_outlet_temperature"].celsius
                representative_temp = (inlet + outlet) / 2

                result = calculate_hydraulics(
                    settings,
                    flow.flow_lpm,
                    temperatures["flowmeter_temperature"].celsius,
                    representative_temp,
                )

                label = "CALIBRATED FLOW" if flow.calibrated else "UNCALIBRATED FLOW"
                print(
                    f"PRELIMINARY | {label} | {flow.flow_lpm:.3f} L/min"
                    f" | coil volume={result['total_volume_litres']:.2f} L"
                    f" | representative T={representative_temp:.1f} °C"
                )
                for number, branch in enumerate(result["branches"], 1):
                    print(
                        f"Branch {number}: "
                        f"{branch['flow_lpm']:.3f} L/min | "
                        f"v={branch['velocity_m_s']:.5f} m/s | "
                        f"Re={branch['reynolds']:.0f} | "
                        f"nominal residence={branch['nominal_residence_min']:.1f} min"
                    )

            except ValueError as error:
                print(f"HYDRAULICS UNAVAILABLE: {error}", flush=True)

            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        rpi.cleanup()


if __name__ == "__main__":
    main()
