"""Synthetic transport, cooling, stop/restart and history tests."""

from copy import deepcopy
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from treatment_model.tracking import CoilTracker, summarize_discharge


def run(settings):
    trackers = [CoilTracker(settings, index) for index in range(2)]
    fractions = settings["hydraulics"]["branch_mass_flow_fractions"]
    target = settings["target_log_reduction"]
    if len(fractions) != 2 or not math.isclose(sum(fractions), 1.0):
        raise ValueError("Demo requires two branch fractions summing to one")
    if any(fraction <= 0 for fraction in fractions):
        raise ValueError("Branch fractions must be positive")

    last_result = None

    print("SYNTHETIC INPUTS — fixed U, constant density, unvalidated")
    print(" min  flow L/min  bath°C  outlet°C  history  target")

    # One record per minute; inputs apply throughout that minute.
    for minute in range(240):
        inlet = 40.0

        if minute < 100:
            flow, bath = 0.5, 80.0
        elif minute < 130:
            flow, bath = 0.5, 50.0
        elif minute < 150:
            flow, bath = 1.0, 50.0
        elif minute < 170:
            flow, bath = 0.0, 80.0
        else:
            flow, bath = 0.5, 80.0

        if minute == 200:
            # Emulate a history loss; no invented exposure during a gap.
            for tracker in trackers:
                tracker.invalidate_history()

        discharged = []
        for tracker, fraction in zip(trackers, fractions):
            discharged.extend(
                tracker.advance(60.0, flow * fraction, inlet, bath)
            )

        result = summarize_discharge(discharged, target)
        last_result = result

        if minute == 99:
            assert result["history_complete"] is True

            # Independent analytical steady outlet at fixed reference flow.
            geometry = settings["geometry"]
            options = settings["tracking"]
            for index, tracker in enumerate(trackers):
                area = (
                    math.pi * geometry["tube_outer_diameter_m"]
                    * geometry["branch_lengths_m"][index]
                )
                mass_flow = (
                    options["reference_density_kg_m3"]
                    * flow * fractions[index] / 60000
                )
                expected = bath - (bath - inlet) * math.exp(
                    -options["fixed_u_outer_w_m2k"] * area
                    / (mass_flow * options["specific_heat_j_kgk"])
                )
                branch_outlet = tracker.parcels[0].temperature_c
                assert abs(branch_outlet - expected) < 0.1

        if 150 <= minute < 170:
            assert result["discharged_volume_l"] == 0
            assert result["model_target_reached"] is None

        if minute == 200:
            assert result["history_complete"] is False

        if minute % 10 == 9 or minute in (100, 130, 150, 170, 200):
            outlet = (
                "NO FLOW" if result["temperature_c"] is None
                else f"{result['temperature_c']:.2f}"
            )
            print(
                f"{minute + 1:4d} {flow:11.2f} {bath:7.1f} "
                f"{outlet:>9} "
                f"{str(result['history_complete']):>8} "
                f"{str(result['model_target_reached']):>7}"
            )

    return last_result


def main():
    with (ROOT / "config/treatment_settings.json").open() as file:
        settings = json.load(file)

    run(settings)
    print("\nVolume, steady-outlet, no-flow and history checks passed.")
    print("Stop-flow heat transfer uses the assumed fixed U.")
    print("The convection coefficient model is not yet connected.")


if __name__ == "__main__":
    main()