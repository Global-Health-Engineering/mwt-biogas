"""Representative convection coefficients for transient tracking.

Heating uses the selected correlations.
Cooling uses a symmetric heat-flow approximation, explicitly flagged.
Zero flow and near-equal mean/bath temperatures require a fallback U.
"""

import math

from scipy.optimize import brentq

from .convection import internal_nusselt, liquid_water


def branch_coefficients(
    settings,
    branch_index,
    branch_flow_lpm,
    bulk_temp_c,
    bath_temp_c,
):
    options = settings["heat_transfer"]
    geometry = settings["geometry"]
    pressure = float(options["property_pressure_mpa"])

    bulk = float(bulk_temp_c)
    bath = float(bath_temp_c)
    flow = float(branch_flow_lpm)

    fluid = liquid_water(bulk, pressure)
    liquid_water(bath, pressure)

    if not math.isfinite(flow) or flow <= 0:
        raise ValueError("Positive branch flow required")
    if abs(bath - bulk) <= 1e-5:
        raise ValueError("Temperature difference too small for coefficient solve")

    outer = float(geometry["tube_outer_diameter_m"])
    bore = outer - 2 * float(geometry["wall_thickness_m"])
    diameter = float(geometry["coil_diameters_m"][branch_index])
    height = float(options["coil_heights_m"][branch_index])
    steel_k = float(options["steel_conductivity_w_mk"])
    fouling = float(options["fouling_resistance_outer_m2k_w"])
    density = float(settings["tracking"]["reference_density_kg_m3"])

    values = (outer, bore, diameter, height, steel_k, density)
    if any(not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError("Invalid geometry or material properties")
    if bore >= outer or not math.isfinite(fouling) or fouling < 0:
        raise ValueError("Invalid bore or fouling resistance")

    # Match the constant-reference-density convention of the tracker.
    mass_flow = density * flow / 60000
    reynolds = 4 * mass_flow / (math.pi * bore * fluid.mu)
    dean = reynolds * math.sqrt(bore / diameter)
    prandtl = fluid.mu * fluid.cp * 1000 / fluid.k
    inside_nu = internal_nusselt(dean, prandtl)
    inside_h = inside_nu * fluid.k / bore

    inside_resistance = outer / (bore * inside_h)
    wall_resistance = outer * math.log(outer / bore) / (2 * steel_k)
    remaining_resistance = inside_resistance + wall_resistance + fouling

    def outside(wall):
        film = liquid_water((bath + wall) / 2, pressure)
        nu = film.mu / film.rho
        alpha = film.k / (film.rho * film.cp * 1000)
        rayleigh = (
            9.81 * film.alfav * abs(bath - wall) * height**3
            / (nu * alpha)
        )
        outside_h = 0.0749 * rayleigh**0.3421 * film.k / height
        return outside_h, rayleigh

    def balance(wall):
        outside_h, _ = outside(wall)
        return (
            outside_h * (bath - wall)
            - (wall - bulk) / remaining_resistance
        )

    wall = brentq(
        balance,
        min(bulk, bath),
        max(bulk, bath),
        xtol=float(options["wall_root_tolerance_c"]),
        maxiter=options["max_iterations"],
    )

    outside_h, rayleigh = outside(wall)
    if outside_h <= 0:
        raise RuntimeError("External coefficient solve became degenerate")

    incoming = outside_h * (bath - wall)
    outgoing = (wall - bulk) / remaining_resistance
    residual = abs(incoming - outgoing)

    if residual > max(1e-4, 0.001 * max(abs(incoming), abs(outgoing))):
        raise RuntimeError("Wall heat-flux balance did not close")

    flags = []
    if not 9e9 <= rayleigh <= 4e11:
        flags.append("External Rayleigh number outside published range")
    if diameter / bore <= 10:
        flags.append("Coil/tube diameter ratio outside preferred geometry")
    if reynolds >= 2000:
        flags.append("Laminar applicability needs review")
    if bath < bulk:
        flags.append("Cooling uses symmetric natural-convection approximation")

    return {
        "overall_u_outer_w_m2k": 1 / (
            remaining_resistance + 1 / outside_h
        ),
        "inside_h_w_m2k": inside_h,
        "outside_h_w_m2k": outside_h,
        "reynolds": reynolds,
        "rayleigh_height": rayleigh,
        "bulk_temperature_c": bulk,
        "outside_wall_temperature_c": wall,
        "flags": flags,
    }