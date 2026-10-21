"""Compare tracking resolution under identical synthetic inputs."""

from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from treatment_model.tracking import CoilTracker, summarize_discharge


def simulate(settings, step_s):
    scenario = deepcopy(settings)
    scenario["tracking"]["max_step_s"] = step_s

    trackers = [CoilTracker(scenario, index) for index in range(2)]
    fractions = scenario["hydraulics"]["branch_mass_flow_fractions"]
    results = []

    for minute in range(240):
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
            for tracker in trackers:
                tracker.invalidate_history()

        discharged = []
        for tracker, fraction in zip(trackers, fractions):
            discharged.extend(
                tracker.advance(60.0, flow * fraction, 40.0, bath)
            )

        results.append(summarize_discharge(
            discharged, scenario["target_log_reduction"]
        ))

    return results


def compare(coarse, fine, label):
    temperature_changes = []
    log_changes = []
    volume_changes = []
    history_changes = []
    target_changes = []

    for minute, (a, b) in enumerate(zip(coarse, fine), 1):
        volume_changes.append(abs(
            a["discharged_volume_l"] - b["discharged_volume_l"]
        ))

        if a["history_complete"] != b["history_complete"]:
            history_changes.append(minute)

        if a["model_target_reached"] != b["model_target_reached"]:
            target_changes.append(minute)

        if a["temperature_c"] is not None and b["temperature_c"] is not None:
            temperature_changes.append((
                abs(a["temperature_c"] - b["temperature_c"]), minute
            ))

        if (
            a["raw_model_log_reduction"] is not None
            and b["raw_model_log_reduction"] is not None
        ):
            log_changes.append((
                abs(
                    a["raw_model_log_reduction"]
                    - b["raw_model_log_reduction"]
                ),
                minute,
            ))

    print(f"\n{label}")
    if temperature_changes:
        difference, minute = max(temperature_changes)
        print(f"Maximum outlet difference: {difference:.6f} °C at minute {minute}")
    if log_changes:
        difference, minute = max(log_changes)
        print(f"Maximum log-reduction difference: {difference:.6f} at minute {minute}")

    print(f"Maximum discharged-volume difference: {max(volume_changes):.3e} L")
    print(f"History-status differences at minutes: {history_changes}")
    print(f"Target-status differences at minutes: {target_changes}")


def main():
    with (ROOT / "config/treatment_settings.json").open() as file:
        settings = json.load(file)

    runs = {}
    for step in (5.0, 2.5, 1.25):
        print(f"Running {step:g}-second internal steps...", flush=True)
        runs[step] = simulate(settings, step)

    compare(runs[5.0], runs[2.5], "5 s versus 2.5 s")
    compare(runs[2.5], runs[1.25], "2.5 s versus 1.25 s")
    print("\nNumerical comparison only; physical validation remains pending.")


if __name__ == "__main__":
    main()