"""Couple coil-average convection coefficients to a steady thermal profile."""

from copy import deepcopy
import math

from .convection import calculate_coefficients, liquid_water
from .hydraulics import calculate_hydraulics
from .thermal import calculate_thermal


def calculate_coupled_thermal(
    settings,
    flow_lpm,
    sensor_temp_c,
    inlet_temp_c,
    bath_temp_c,
):
    options = settings["heat_transfer"]
    pressure = options["property_pressure_mpa"]
    inlet = float(inlet_temp_c)
    bath = float(bath_temp_c)

    liquid_water(inlet, pressure)
    liquid_water(bath, pressure)
    if bath <= inlet:
        raise ValueError(
            "This initial coupled model requires bath temperature > inlet"
        )

    bulk_tolerance = float(options["bulk_temperature_tolerance_c"])
    outlet_tolerance = float(options["outlet_temperature_tolerance_c"])
    relaxation = float(options["relaxation_factor"])
    max_iterations = options["max_iterations"]

    if any(
        not math.isfinite(x) or x <= 0
        for x in (bulk_tolerance, outlet_tolerance)
    ):
        raise ValueError("Temperature tolerances must be finite and positive")
    if not math.isfinite(relaxation) or not 0 < relaxation <= 1:
        raise ValueError("Relaxation factor must be in (0, 1]")
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer")

    cp = float(settings["thermal"]["specific_heat_j_kgk"])
    if not math.isfinite(cp) or cp <= 0:
        raise ValueError("Specific heat must be finite and positive")

    # Preserve the mass-flow convention used by the existing modules.
    hydraulics = calculate_hydraulics(
        settings, flow_lpm, sensor_temp_c, inlet
    )
    total_mass = hydraulics["mass_flow_kg_s"]

    geometry = settings["geometry"]
    lengths = geometry["branch_lengths_m"]
    fractions = settings["hydraulics"]["branch_mass_flow_fractions"]
    outer = float(geometry["tube_outer_diameter_m"])

    branch_solutions = []

    for index, (length, fraction) in enumerate(zip(lengths, fractions)):
        branch_mass = total_mass * float(fraction)
        outside_area = math.pi * outer * float(length)
        bulk_guess = (inlet + bath) / 2
        previous_outlet = None

        for iteration in range(1, max_iterations + 1):
            # Avoid numerical degeneracy as the driving difference vanishes.
            if bath - bulk_guess <= 1e-5:
                raise RuntimeError(
                    f"Branch {index + 1}: temperature difference too small "
                    "for the present coefficient solver"
                )

            # The existing function evaluates all branches at one bulk T.
            # Select this branch; each branch has its own iteration.
            coefficients = calculate_coefficients(
                settings,
                flow_lpm,
                sensor_temp_c,
                bulk_guess,
                bath,
            )["branches"][index]

            overall_u = coefficients["overall_u_outer_w_m2k"]
            ntu = overall_u * outside_area / (branch_mass * cp)

            if not math.isfinite(ntu) or ntu <= 0:
                raise RuntimeError("Invalid branch NTU")

            rise_fraction = -math.expm1(-ntu)
            outlet = inlet + (bath - inlet) * rise_fraction

            # Exact length-average temperature for constant U, cp and bath:
            # T(x) = bath - (bath - inlet) exp(-NTU*x/L).
            # This is not simply (inlet + outlet)/2.
            mean_deficit_fraction = rise_fraction / ntu
            predicted_bulk = (
                bath - (bath - inlet) * mean_deficit_fraction
            )

            bulk_residual = abs(predicted_bulk - bulk_guess)
            outlet_change = (
                math.inf if previous_outlet is None
                else abs(outlet - previous_outlet)
            )

            if (
                bulk_residual <= bulk_tolerance
                and outlet_change <= outlet_tolerance
            ):
                branch_solutions.append({
                    "representative_bulk_temperature_c": bulk_guess,
                    "profile_mean_temperature_c": predicted_bulk,
                    "bulk_residual_c": bulk_residual,
                    "outlet_change_c": outlet_change,
                    "analytical_outlet_temperature_c": outlet,
                    "iterations": iteration,
                    "coefficients": coefficients,
                })
                break

            previous_outlet = outlet
            bulk_guess += relaxation * (predicted_bulk - bulk_guess)

        else:
            raise RuntimeError(
                f"Branch {index + 1}: coupled solver did not converge "
                f"within {max_iterations} iterations"
            )

    # Use the converged branch coefficients in the existing profile solver.
    scenario = deepcopy(settings)
    scenario["thermal"]["overall_u_outer_w_m2k"] = [
        branch["coefficients"]["overall_u_outer_w_m2k"]
        for branch in branch_solutions
    ]
    scenario["thermal"]["coefficient_basis"] = (
        "Manlapaz_Churchill_1981_eq39_and_Prabhanjan_2004"
    )

    result = calculate_thermal(
        scenario, flow_lpm, sensor_temp_c, inlet, bath
    )
    result["model"] = "steady_uniform_bath_coupled_average_coefficients"
    result["numerically_converged"] = True
    result["model_validated"] = False
    result["flags"] = []

    for index, (profile_branch, solution) in enumerate(
        zip(result["branches"], branch_solutions), 1
    ):
        predicted = solution["analytical_outlet_temperature_c"]
        if not math.isclose(
            profile_branch["outlet_temperature_c"],
            predicted,
            abs_tol=1e-8,
        ):
            raise RuntimeError("Profile and analytical outlet disagree")

        profile_branch["coefficient_solution"] = solution

        for flag in solution["coefficients"]["flags"]:
            result["flags"].append(f"Branch {index}: {flag}")

    # Independent mixed-stream energy balance under the constant-cp model.
    expected_heat = total_mass * cp * (
        result["mixed_outlet_temperature_c"] - inlet
    )
    heat_residual = abs(result["heat_to_effluent_w"] - expected_heat)

    if heat_residual > max(0.1, 0.001 * abs(expected_heat)):
        raise RuntimeError("Mixed-stream heat balance did not close")

    result["heat_balance_residual_w"] = heat_residual
    return result