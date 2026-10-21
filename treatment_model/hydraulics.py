"""Steady-flow estimates; not actual parcel residence-time tracking."""

import math

from .properties import water_properties


def calculate_hydraulics(settings, flow_lpm, sensor_temp_c, coil_temp_c):
    """Use measured flow at sensor temperature and a representative coil T."""
    flow_lpm = float(flow_lpm)
    if not math.isfinite(flow_lpm) or flow_lpm <= 0:
        raise ValueError("Positive effluent flow required")

    geometry = settings["geometry"]
    if geometry["connection"] != "parallel":
        raise ValueError("Only parallel coils are implemented")

    outer = float(geometry["tube_outer_diameter_m"])
    wall = float(geometry["wall_thickness_m"])
    lengths = [float(x) for x in geometry["branch_lengths_m"]]
    fractions = [
        float(x)
        for x in settings["hydraulics"]["branch_mass_flow_fractions"]
    ]

    if not all(math.isfinite(x) for x in [outer, wall, *lengths, *fractions]):
        raise ValueError("Geometry and flow fractions must be finite")
    if outer <= 0 or wall <= 0 or 2 * wall >= outer:
        raise ValueError("Invalid tube dimensions")
    if not lengths or len(lengths) != len(fractions):
        raise ValueError("Each branch needs a length and flow fraction")
    if any(x <= 0 for x in lengths + fractions):
        raise ValueError("Lengths and branch fractions must be positive")
    if not math.isclose(sum(fractions), 1.0, abs_tol=1e-9):
        raise ValueError("Branch flow fractions must sum to one")

    bore = outer - 2 * wall
    area = math.pi * bore**2 / 4
    sensor_density, _ = water_properties(sensor_temp_c)
    coil_density, viscosity = water_properties(coil_temp_c)

    mass_flow = sensor_density * flow_lpm / 60000.0
    branches = []

    for length, fraction in zip(lengths, fractions):
        branch_mass_flow = mass_flow * fraction
        volume_flow = branch_mass_flow / coil_density
        velocity = volume_flow / area
        volume = area * length
        reynolds = coil_density * velocity * bore / viscosity

        branches.append({
            "volume_litres": volume * 1000,
            "flow_lpm": volume_flow * 60000,
            "velocity_m_s": velocity,
            "reynolds": reynolds,
            "nominal_residence_min": volume / volume_flow / 60,
        })

    return {
        "bore_m": bore,
        "mass_flow_kg_s": mass_flow,
        "total_volume_litres": sum(b["volume_litres"] for b in branches),
        "branches": branches,
    }
