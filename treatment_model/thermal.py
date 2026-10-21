"""Preliminary steady-state parallel-coil thermal model.

Uniform prescribed bath temperature; constant cp and branch U.
No transient history, radial gradients or microbiological prediction.
U is defined on the outside tube area.
"""

import math

from .hydraulics import calculate_hydraulics
from .properties import water_properties


def calculate_thermal(
    settings,
    flow_lpm,
    sensor_temp_c,
    inlet_temp_c,
    bath_temp_c,
):
    inlet = float(inlet_temp_c)
    bath = float(bath_temp_c)

    # Enforce the available liquid-water property range.
    water_properties(inlet)
    water_properties(bath)

    thermal = settings["thermal"]
    if thermal["model"] != "steady_uniform_bath":
        raise ValueError("Unsupported thermal model")

    segments = thermal["segments_per_branch"]
    if type(segments) is not int or segments < 1:
        raise ValueError("segments_per_branch must be a positive integer")

    cp = float(thermal["specific_heat_j_kgk"])
    if not math.isfinite(cp) or cp <= 0:
        raise ValueError("Specific heat must be finite and positive")

    geometry = settings["geometry"]
    lengths = geometry["branch_lengths_m"]
    coefficients = [
        float(value)
        for value in thermal["overall_u_outer_w_m2k"]
    ]
    fractions = settings["hydraulics"]["branch_mass_flow_fractions"]

    if len(coefficients) != len(lengths):
        raise ValueError("One heat-transfer coefficient needed per branch")
    if any(not math.isfinite(u) or u < 0 for u in coefficients):
        raise ValueError("Heat-transfer coefficients must be finite and nonnegative")

    # Reuse geometry, flow-fraction and positive-flow validation.
    hydraulics = calculate_hydraulics(
        settings, flow_lpm, sensor_temp_c, inlet
    )
    mass_flow = hydraulics["mass_flow_kg_s"]
    bore = hydraulics["bore_m"]
    outer = float(geometry["tube_outer_diameter_m"])
    cross_section = math.pi * bore**2 / 4

    branches = []

    for length, fraction, coefficient in zip(
        lengths, fractions, coefficients
    ):
        length = float(length)
        branch_mass_flow = mass_flow * float(fraction)
        dx = length / segments
        outside_segment_area = math.pi * outer * dx
        ntu_segment = (
            coefficient * outside_segment_area / (branch_mass_flow * cp)
        )

        temperature = inlet
        elapsed = 0.0
        profile = [{
            "distance_m": 0.0,
            "travel_time_s": 0.0,
            "temperature_c": inlet,
        }]

        for index in range(segments):
            # Exact segment solution under constant U, cp and bath T.
            rise_fraction = -math.expm1(-ntu_segment)
            next_temperature = (
                temperature + (bath - temperature) * rise_fraction
            )

            midpoint_temperature = (
                temperature
                + (bath - temperature)
                * (-math.expm1(-ntu_segment / 2))
            )
            density, _ = water_properties(midpoint_temperature)

            # Local fluid inventory / branch mass flow.
            elapsed += density * cross_section * dx / branch_mass_flow
            temperature = next_temperature

            profile.append({
                "distance_m": (index + 1) * dx,
                "travel_time_s": elapsed,
                "temperature_c": temperature,
            })

        branches.append({
            "mass_flow_kg_s": branch_mass_flow,
            "u_outer_w_m2k": coefficient,
            "outlet_temperature_c": temperature,
            "heat_to_effluent_w": (
                branch_mass_flow * cp * (temperature - inlet)
            ),
            "nominal_travel_time_s": elapsed,
            "profile": profile,
        })

    # Mass-weighted temperature is valid here because cp is constant
    # and identical for both branches.
    mixed_outlet = sum(
        branch["mass_flow_kg_s"] * branch["outlet_temperature_c"]
        for branch in branches
    ) / mass_flow

    return {
        "model_validated": False,
        "model": "steady_uniform_bath",
        "coefficient_basis": thermal["coefficient_basis"],
        "bath_temperature_c": bath,
        "mixed_outlet_temperature_c": mixed_outlet,
        "heat_to_effluent_w": sum(
            branch["heat_to_effluent_w"] for branch in branches
        ),
        "branches": branches,
        "ecoli_log_reduction": None,
    }