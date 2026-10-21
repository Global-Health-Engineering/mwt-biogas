"""Offline thermal scenarios. No hardware access."""

from copy import deepcopy
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from treatment_model.thermal import calculate_thermal


def main():
    with (ROOT / "config/treatment_settings.json").open() as file:
        settings = json.load(file)

    # Explicit simulated inputs, independent of live sensor readings.
    flow_lpm = 0.5
    sensor_temp = 20.0
    inlet_temp = 40.0
    bath_temp = 80.0

    print("SIMULATED steady operation; assumed U; no treatment prediction.")
    print("Flow=0.5 L/min at 20 °C; coil inlet=40 °C; bath=80 °C")

    for coefficient in (10.0, 25.0, 50.0):
        scenario = deepcopy(settings)
        count = len(scenario["geometry"]["branch_lengths_m"])
        scenario["thermal"]["overall_u_outer_w_m2k"] = (
            [coefficient] * count
        )

        result = calculate_thermal(
            scenario, flow_lpm, sensor_temp, inlet_temp, bath_temp
        )

        print(
            f"\nU={coefficient:.1f} W/(m²·K)"
            f" | mixed outlet={result['mixed_outlet_temperature_c']:.3f} °C"
            f" | heat={result['heat_to_effluent_w']:.1f} W"
        )

        for number, branch in enumerate(result["branches"], 1):
            print(
                f"  Branch {number}: "
                f"outlet={branch['outlet_temperature_c']:.3f} °C"
                f" | nominal travel="
                f"{branch['nominal_travel_time_s'] / 60:.2f} min"
            )

            # Independent analytical outlet check.
            length = scenario["geometry"]["branch_lengths_m"][number - 1]
            outer = scenario["geometry"]["tube_outer_diameter_m"]
            cp = scenario["thermal"]["specific_heat_j_kgk"]
            expected = bath_temp - (bath_temp - inlet_temp) * math.exp(
                -coefficient * math.pi * outer * length
                / (branch["mass_flow_kg_s"] * cp)
            )
            assert math.isclose(
                branch["outlet_temperature_c"], expected, abs_tol=1e-9
            )

        total_mass_flow = sum(
            branch["mass_flow_kg_s"] for branch in result["branches"]
        )
        expected_heat = (
            total_mass_flow
            * scenario["thermal"]["specific_heat_j_kgk"]
            * (result["mixed_outlet_temperature_c"] - inlet_temp)
        )
        assert math.isclose(
            result["heat_to_effluent_w"], expected_heat, abs_tol=1e-8
        )

    # Zero transfer must preserve the inlet temperature.
    insulated = deepcopy(settings)
    insulated["thermal"]["overall_u_outer_w_m2k"] = [
        0.0
    ] * len(insulated["geometry"]["branch_lengths_m"])
    result = calculate_thermal(
        insulated, flow_lpm, sensor_temp, inlet_temp, bath_temp
    )
    assert math.isclose(
        result["mixed_outlet_temperature_c"], inlet_temp, abs_tol=1e-10
    )
    assert result["heat_to_effluent_w"] == 0.0

    print("\nAnalytical temperature and heat-balance checks passed.")


if __name__ == "__main__":
    main()