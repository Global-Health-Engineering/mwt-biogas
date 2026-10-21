"""Offline checks and combined steady thermal/kinetic demonstration."""

import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from treatment_model.coupled_thermal import calculate_coupled_thermal
from treatment_model.kinetics import (
    calculate_reduction,
    mix_log_reductions,
    profile_log_reduction,
)


def constant_profile(temperature, seconds):
    return [
        {"travel_time_s": 0.0, "temperature_c": temperature},
        {"travel_time_s": seconds, "temperature_c": temperature},
    ]


def main():
    with (ROOT / "config/treatment_settings.json").open() as file:
        settings = json.load(file)

    # These checks assume the agreed original Ziemba rate table.
    target_seconds = 3600 * math.log(1000) / 650
    for temperature in (60.0, 80.0):
        result = profile_log_reduction(
            settings, constant_profile(temperature, target_seconds)
        )
        assert math.isclose(
            result["raw_model_log_reduction"], 3.0, abs_tol=1e-10
        )

    cold = profile_log_reduction(settings, constant_profile(40.0, 3600))
    assert cold["raw_model_log_reduction"] == 0.0

    ramp = [
        {"travel_time_s": 0.0, "temperature_c": 45.0},
        {"travel_time_s": 20.0, "temperature_c": 65.0},
    ]
    expected = (1.4 * 5 + 6.8 * 5 + 650 * 5) / 3600 / math.log(10)
    assert math.isclose(
        profile_log_reduction(settings, ramp)["raw_model_log_reduction"],
        expected,
        abs_tol=1e-10,
    )

    # Half untreated and half extremely treated gives only ~0.301 logs.
    assert math.isclose(
        mix_log_reductions([0.0, 1000.0], [0.5, 0.5]),
        math.log10(2),
        abs_tol=1e-10,
    )

    print("Constant-temperature, threshold and mixing checks passed.")

    thermal = calculate_coupled_thermal(
        settings,
        flow_lpm=0.5,
        sensor_temp_c=20.0,
        inlet_temp_c=40.0,
        bath_temp_c=80.0,
    )
    reduction = calculate_reduction(settings, thermal)

    print("\nSIMULATED STEADY OPERATION — UNVALIDATED")
    print(f"Outlet: {thermal['mixed_outlet_temperature_c']:.3f} °C")

    for number, branch in enumerate(reduction["branches"], 1):
        print(
            f"Branch {number}: "
            f"time above 60 °C={branch['time_above_rate_cap_s'] / 60:.2f} min"
        )

    print(
        f"Credited reduction, capped at target: "
        f"{reduction['credited_log_reduction']:.3f} logs "
        f"({reduction['credited_reduction_percent']:.3f}%)"
    )
    print(f"Model target reached: {reduction['model_target_reached']}")
    print("This flag does not authorize effluent release.")

    for flag in reduction["flags"]:
        print(f"  {flag}")


if __name__ == "__main__":
    main()