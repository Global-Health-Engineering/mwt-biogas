"""Preliminary coil-average thermal resistance calculation.

Internal: Manlapaz & Churchill (1981), Eq. 39, He approximated by De.
External: Prabhanjan et al. (2004), coil-height correlation.
Equal-flow assumptions are supplied through the existing configuration.

Internal properties are evaluated at the supplied bulk temperature.
External properties are evaluated at the bath/wall film temperature.
No buoyancy correction inside the tube or pumped external-flow correction.
"""

import math

from iapws import IAPWS97
from scipy.optimize import brentq

from .hydraulics import calculate_hydraulics


def liquid_water(temperature_c, pressure_mpa):
    temperature = float(temperature_c)
    pressure = float(pressure_mpa)

    if not math.isfinite(temperature) or not 10 <= temperature <= 90:
        raise ValueError("Model temperature must be between 10 and 90 °C")
    if not math.isfinite(pressure) or pressure <= 0:
        raise ValueError("Property pressure must be finite and positive")

    water = IAPWS97(T=temperature + 273.15, P=pressure)

    if water.region != 1:
        raise ValueError("Liquid-water properties required")

    return water


def internal_nusselt(dean, prandtl):
    """Manlapaz & Churchill Eq. 39, using He = De."""
    if dean < 0 or prandtl <= 0:
        raise ValueError("Invalid Dean or Prandtl number")
    if dean == 0:
        return 3.657

    first = 3.657 + 4.343 / (
        1 + 957 / (prandtl * dean**2)
    )**2
    second = 1.158 * (dean / (1 + 0.477 / prandtl))**1.5
    return (first**3 + second)**(1 / 3)


def calculate_coefficients(
    settings,
    flow_lpm,
    sensor_temp_c,
    bulk_temp_c,
    bath_temp_c,
):
    """Return coefficients for a specified representative bulk temperature.

    This is a coefficient calculation, not yet a self-consistent
    prediction of the bulk temperature or outlet temperature.
    """
    options = settings["heat_transfer"]
    pressure = float(options["property_pressure_mpa"])
    bulk = float(bulk_temp_c)
    bath = float(bath_temp_c)

    fluid = liquid_water(bulk, pressure)
    liquid_water(bath, pressure)

    if bath <= bulk:
        raise ValueError("This initial implementation requires bath > bulk")

    hydraulic = calculate_hydraulics(
        settings, flow_lpm, sensor_temp_c, bulk
    )

    geometry = settings["geometry"]
    outer = float(geometry["tube_outer_diameter_m"])
    bore = hydraulic["bore_m"]
    diameters = [float(x) for x in geometry["coil_diameters_m"]]
    heights = [float(x) for x in options["coil_heights_m"]]
    fractions = settings["hydraulics"]["branch_mass_flow_fractions"]

    conductivity = float(options["steel_conductivity_w_mk"])
    fouling = float(options["fouling_resistance_outer_m2k_w"])
    tolerance = float(options["wall_root_tolerance_c"])
    iterations = options["max_iterations"]

    count = len(hydraulic["branches"])
    if len(diameters) != count or len(heights) != count:
        raise ValueError("Each branch needs a coil diameter and height")

    positive = [conductivity, tolerance, *diameters, *heights]
    if any(not math.isfinite(x) or x <= 0 for x in positive):
        raise ValueError("Dimensions, conductivity and tolerance must be positive")
    if not math.isfinite(fouling) or fouling < 0:
        raise ValueError("Fouling resistance must be finite and nonnegative")
    if type(iterations) is not int or iterations < 1:
        raise ValueError("max_iterations must be a positive integer")

    prandtl = fluid.mu * fluid.cp * 1000 / fluid.k
    wall_resistance = (
        outer * math.log(outer / bore) / (2 * conductivity)
    )

    results = []

    for diameter, height, fraction in zip(diameters, heights, fractions):
        branch_mass = hydraulic["mass_flow_kg_s"] * float(fraction)
        reynolds = 4 * branch_mass / (math.pi * bore * fluid.mu)
        dean = reynolds * math.sqrt(bore / diameter)
        nusselt = internal_nusselt(dean, prandtl)
        inside_h = nusselt * fluid.k / bore

        inside_resistance = outer / (bore * inside_h)
        remaining_resistance = (
            inside_resistance + wall_resistance + fouling
        )

        def outside_coefficient(wall_temperature):
            film = liquid_water((bath + wall_temperature) / 2, pressure)
            delta = bath - wall_temperature

            kinematic_viscosity = film.mu / film.rho
            diffusivity = film.k / (film.rho * film.cp * 1000)
            rayleigh = (
                9.81 * film.alfav * delta * height**3
                / (kinematic_viscosity * diffusivity)
            )

            # At zero temperature difference the correlated heat flux
            # vanishes. This endpoint is used only to bracket the root.
            outside_nu = 0.0749 * rayleigh**0.3421
            outside_h = outside_nu * film.k / height
            return outside_h, rayleigh

        def imbalance(wall_temperature):
            outside_h, _ = outside_coefficient(wall_temperature)
            incoming_flux = outside_h * (bath - wall_temperature)
            outgoing_flux = (
                wall_temperature - bulk
            ) / remaining_resistance
            return incoming_flux - outgoing_flux

        wall_temperature = brentq(
            imbalance,
            bulk,
            bath,
            xtol=tolerance,
            maxiter=iterations,
        )

        outside_h, rayleigh = outside_coefficient(wall_temperature)
        overall_u = 1 / (
            remaining_resistance + 1 / outside_h
        )

        incoming = outside_h * (bath - wall_temperature)
        outgoing = (wall_temperature - bulk) / remaining_resistance
        residual = abs(incoming - outgoing)

        if residual > max(1e-4, 0.001 * max(abs(incoming), abs(outgoing))):
            raise RuntimeError("Wall heat-flux balance did not close")

        flags = []
        if not 9e9 <= rayleigh <= 4e11:
            flags.append("External Rayleigh number outside published range")
        if diameter / bore <= 10:
            flags.append("Coil/tube diameter ratio is outside preferred geometry")
        if reynolds >= 2000:
            flags.append(
                "Laminar applicability needs review; "
                "2000 is a screening threshold, not a coil transition criterion"
            )

        results.append({
            "reynolds": reynolds,
            "dean": dean,
            "prandtl": prandtl,
            "internal_nusselt": nusselt,
            "inside_h_w_m2k": inside_h,
            "outside_h_w_m2k": outside_h,
            "overall_u_outer_w_m2k": overall_u,
            "outside_wall_temperature_c": wall_temperature,
            "rayleigh_height": rayleigh,
            "heat_flux_w_m2": incoming,
            "heat_flux_residual_w_m2": residual,
            "flags": flags,
        })

    return {
        "model_validated": False,
        "branches": results,
        "assumptions": [
            "Representative bulk temperature supplied",
            "He approximated by De",
            "Internal properties evaluated at bulk temperature",
            "Coil-average external natural convection",
            "Constant steel conductivity",
            "Pump and adjacent-coil effects not resolved",
            "Fouling treated as a lumped outside-area resistance",
        ],
    }