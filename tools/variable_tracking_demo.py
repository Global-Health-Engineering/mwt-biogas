"""Short offline checks of the variable-coefficient connection."""

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from treatment_model.tracking import summarize_discharge
from treatment_model.variable_tracking import VariableCoilTracker


def main():
    with (ROOT / "config/treatment_settings.json").open() as file:
        settings = json.load(file)

    trackers = [
        VariableCoilTracker(settings, index)
        for index in range(2)
    ]
    fractions = settings["hydraulics"]["branch_mass_flow_fractions"]

    print("OFFLINE CONNECTION TEST — UNVALIDATED")
    print("Short test: initial-fluid history will remain incomplete.")

    cases = [
        ("Heating", 0.5, 80.0),
        ("Cooling", 0.5, 10.0),
        ("Stopped", 0.0, 80.0),
        ("Restart", 0.5, 80.0),
    ]

    for label, total_flow, bath in cases:
        start = time.perf_counter()
        discharged = []

        for tracker, fraction in zip(trackers, fractions):
            discharged.extend(
                tracker.advance(
                    duration_s=60.0,
                    branch_flow_lpm=total_flow * fraction,
                    inlet_c=40.0,
                    bath_c=bath,
                )
            )

        elapsed = time.perf_counter() - start
        result = summarize_discharge(
            discharged, settings["target_log_reduction"]
        )

        print(f"\n{label}: bath={bath:.1f} °C, flow={total_flow:.2f} L/min")
        print(f"  Computation for 60 simulated seconds: {elapsed:.3f} s")
        print(f"  Outlet: {result['temperature_c']}")
        print(f"  Complete history: {result['history_complete']}")

        for number, tracker in enumerate(trackers, 1):
            print(f"  Branch {number}: U={tracker.u:.2f} W/(m²·K)")
            for flag in tracker.current_flags:
                print(f"    {flag}")

        if total_flow == 0:
            assert result["discharged_volume_l"] == 0
            assert result["model_target_reached"] is None

    print("\nConnection test completed.")
    print("Runtime here is not representative of a fully populated tracker.")
    print("Retained warnings:")
    for flag in sorted(set.union(
        *(tracker.encountered_flags for tracker in trackers)
    )):
        print(f"  {flag}")


if __name__ == "__main__":
    main()