"""Provisional Ziemba exposure-credit model for culturable E. coli.

Stepwise rates; no credit below the first temperature.
Highest rate is retained above the highest tabulated temperature.
Profiles are linearly interpolated in temperature versus travel time.
No hardware access or treatment-control authorization.
"""

import math


def _rate_table(settings):
    options = settings["kinetics"]
    if options["model"] != "ziemba_stepwise_capped":
        raise ValueError("Unsupported kinetic model")
    if options["below_table"] != "zero_credit":
        raise ValueError("Unsupported below-table rule")
    if options["above_table"] != "cap_at_highest_rate":
        raise ValueError("Unsupported above-table rule")

    table = [
        (float(row["temperature_c"]), float(row["k_per_hour"]))
        for row in options["rate_table"]
    ]
    if not table:
        raise ValueError("Kinetic rate table is empty")

    for temperature, rate in table:
        if not math.isfinite(temperature):
            raise ValueError("Rate-table temperatures must be finite")
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("Rates must be finite and nonnegative")

    if any(b[0] <= a[0] for a, b in zip(table, table[1:])):
        raise ValueError("Temperatures must be strictly increasing")
    if any(b[1] < a[1] for a, b in zip(table, table[1:])):
        raise ValueError("Stepwise rates must be nondecreasing")

    return table


def profile_log_reduction(settings, profile):
    """Integrate k(T) dt and convert natural-log dose to decimal logs."""
    table = _rate_table(settings)

    points = [
        (float(point["travel_time_s"]), float(point["temperature_c"]))
        for point in profile
    ]
    if len(points) < 2:
        raise ValueError("At least two profile points are required")
    if any(
        not math.isfinite(time) or not math.isfinite(temp) or time < 0
        for time, temp in points
    ):
        raise ValueError("Profile requires finite temperatures and nonnegative times")
    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise ValueError("Profile times must be strictly increasing")

    dose = 0.0
    capped_seconds = 0.0
    uncredited_seconds = 0.0

    for (time0, temp0), (time1, temp1) in zip(points, points[1:]):
        cuts = [0.0, 1.0]

        # Split intervals at rate discontinuities. This avoids averaging
        # the 55 and 60 °C rates across a threshold.
        if temp1 != temp0:
            for threshold, _ in table:
                fraction = (threshold - temp0) / (temp1 - temp0)
                if 0 < fraction < 1:
                    cuts.append(fraction)

        cuts.sort()

        for start, end in zip(cuts, cuts[1:]):
            temperature = temp0 + (temp1 - temp0) * (start + end) / 2
            seconds = (time1 - time0) * (end - start)
            rate = 0.0

            for threshold, candidate in table:
                if temperature >= threshold:
                    rate = candidate
                else:
                    break

            dose += rate * seconds / 3600.0

            if temperature > table[-1][0]:
                capped_seconds += seconds
            if temperature < table[0][0]:
                uncredited_seconds += seconds

    return {
        "raw_model_log_reduction": dose / math.log(10),
        "time_above_rate_cap_s": capped_seconds,
        "time_below_credit_threshold_s": uncredited_seconds,
    }


def mix_log_reductions(log_reductions, mass_fractions):
    """Mix surviving fractions without underflow for large log reductions."""
    logs = [float(value) for value in log_reductions]
    weights = [float(value) for value in mass_fractions]

    if not logs or len(logs) != len(weights):
        raise ValueError("One mass fraction is required per branch")
    if any(not math.isfinite(value) or value < 0 for value in logs):
        raise ValueError("Log reductions must be finite and nonnegative")
    if any(not math.isfinite(value) or value <= 0 for value in weights):
        raise ValueError("Mass fractions must be finite and positive")
    if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
        raise ValueError("Mass fractions must sum to one")

    total = sum(weights)
    minimum = min(logs)
    scaled_survival = sum(
        (weight / total) * 10 ** (-(value - minimum))
        for value, weight in zip(logs, weights)
    )
    return max(0.0, minimum - math.log10(scaled_survival))


def calculate_reduction(settings, thermal_result):
    """Evaluate steady profiles; target flag is a model result only."""
    if thermal_result.get("numerically_converged") is not True:
        raise ValueError("A converged coupled thermal result is required")

    target = float(settings["target_log_reduction"])
    if not math.isfinite(target) or target <= 0:
        raise ValueError("Target log reduction must be finite and positive")

    branches = [
        profile_log_reduction(settings, branch["profile"])
        for branch in thermal_result["branches"]
    ]
    mixed_log = mix_log_reductions(
        [branch["raw_model_log_reduction"] for branch in branches],
        settings["hydraulics"]["branch_mass_flow_fractions"],
    )

    # Limit the displayed credit to the target. Large mathematical
    # reductions are retained only as diagnostic model outputs.
    credited_log = min(mixed_log, target)
    credited_percent = -100 * math.expm1(-credited_log * math.log(10))

    flags = list(thermal_result.get("flags", []))
    flags.append("Kinetics and complete process model are not validated")
    if any(branch["time_above_rate_cap_s"] > 0 for branch in branches):
        flags.append("Temperatures above 60 °C credited at the 60 °C rate")

    return {
        "model": settings["kinetics"]["model"],
        "model_validated": False,
        "target_log_reduction": target,
        "raw_model_log_reduction": mixed_log,
        "credited_log_reduction": credited_log,
        "credited_reduction_percent": credited_percent,
        "model_target_reached": mixed_log >= target,
        "branches": branches,
        "flags": flags,
    }