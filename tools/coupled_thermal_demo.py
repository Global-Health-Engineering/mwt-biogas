"""Offline coupled thermal calculation and spatial-resolution check."""

from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from treatment_model.coupled_thermal import calculate_coupled_thermal


def main():
    with (ROOT / "config/treatment_settings.json").open() as file:
        settings = json.load(file)

    print("SIMULATED INPUTS — not validated for treatment control")
    print("Flow: 0.5 L/min at 20 °C")
    print("Effluent inlet: 40 °C; prescribed bath: 80 °C")
    print("In live use, the provisional bath estimate will come from PT5.")

    result = calculate_coupled_thermal(
        settings,
        flow_lpm=0.5,
        sensor_temp_c=20.0,
        inlet_temp_c=40.0,
        bath_temp_c=80.0,
    )

    print(
        f"\nMixed outlet: "
        f"{result['mixed_outlet_temperature_c']:.4f} °C"
        f"\nHeat to effluent: {result['heat_to_effluent_w']:.2f} W"
        f"\nHeat-balance residual: "
        f"{result['heat_balance_residual_w']:.3e} W"
    )

    for number, branch in enumerate(result["branches"], 1):
        solution = branch["coefficient_solution"]
        coefficients = solution["coefficients"]

        print(f"\nBranch {number}")
        print(f"  Iterations: {solution['iterations']}")
        print(
            f"  Mean bulk T: "
            f"{solution['profile_mean_temperature_c']:.3f} °C"
        )
        print(f"  Bulk residual: {solution['bulk_residual_c']:.5f} °C")
        print(
            f"  Uo: {coefficients['overall_u_outer_w_m2k']:.2f}"
            " W/(m²·K)"
        )
        print(
            f"  hi / ho: {coefficients['inside_h_w_m2k']:.2f}"
            f" / {coefficients['outside_h_w_m2k']:.2f} W/(m²·K)"
        )
        print(f"  Ra: {coefficients['rayleigh_height']:.3e}")
        print(f"  Outlet: {branch['outlet_temperature_c']:.4f} °C")
        print(
            f"  Nominal travel: "
            f"{branch['nominal_travel_time_s'] / 60:.3f} min"
        )

    print("\nCorrelation flags:")
    for flag in result["flags"]:
        print(f"  {flag}")
    if not result["flags"]:
        print("  No implemented range flags triggered.")
    print("  Absence of flags does not establish model validity.")

    fine_settings = deepcopy(settings)
    fine_settings["thermal"]["segments_per_branch"] *= 2
    fine = calculate_coupled_thermal(
        fine_settings, 0.5, 20.0, 40.0, 80.0
    )

    print("\nDoubling the segment count:")
    for number, (coarse_branch, fine_branch) in enumerate(
        zip(result["branches"], fine["branches"]), 1
    ):
        time_change = abs(
            fine_branch["nominal_travel_time_s"]
            - coarse_branch["nominal_travel_time_s"]
        )
        print(
            f"  Branch {number}: travel-time change={time_change:.4f} s"
        )

        # Compare temperatures at matching axial positions.
        coarse_profile = coarse_branch["profile"]
        fine_profile = fine_branch["profile"]
        profile_difference = max(
            abs(point["temperature_c"] - fine_profile[2 * i]["temperature_c"])
            for i, point in enumerate(coarse_profile)
        )
        assert profile_difference < 1e-8

    print("\nProfile consistency checks passed.")
    print("E. coli reduction remains unavailable.")


if __name__ == "__main__":
    main()