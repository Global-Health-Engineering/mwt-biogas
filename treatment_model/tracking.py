"""Transient plug-flow parcel tracking with a prescribed constant U.

Constant reference density/cp; no axial mixing or wall heat storage.
Transport uses symmetric half-step displacement around thermal exposure.
This is a numerical tracking baseline, not a validated process model.
"""

from collections import deque
from dataclasses import dataclass
import math

from .kinetics import profile_log_reduction, mix_log_reductions


@dataclass
class Parcel:
    volume_l: float
    temperature_c: float
    log_reduction: float
    history_complete: bool


class CoilTracker:
    def __init__(self, settings, branch_index):
        self.settings = settings
        geometry = settings["geometry"]
        options = settings["tracking"]

        length = float(geometry["branch_lengths_m"][branch_index])
        outer = float(geometry["tube_outer_diameter_m"])
        bore = outer - 2 * float(geometry["wall_thickness_m"])

        self.density = float(options["reference_density_kg_m3"])
        self.cp = float(options["specific_heat_j_kgk"])
        self.u = float(options["fixed_u_outer_w_m2k"])
        self.max_step = float(options["max_step_s"])
        initial = float(options["initial_temperature_c"])

        positive = (length, outer, bore, self.density, self.cp, self.max_step)
        if any(not math.isfinite(x) or x <= 0 for x in positive):
            raise ValueError("Invalid tracker geometry or properties")
        if bore >= outer:
            raise ValueError("Bore must be smaller than outside diameter")
        if not math.isfinite(self.u) or self.u < 0:
            raise ValueError("U must be finite and nonnegative")
        if not math.isfinite(initial):
            raise ValueError("Initial temperature must be finite")

        volume_m3 = math.pi * bore**2 / 4 * length
        outside_area = math.pi * outer * length
        self.capacity_l = volume_m3 * 1000

        # Every parcel has the same area-to-volume ratio.
        self.heating_rate = (
            self.u * outside_area / (self.density * self.cp * volume_m3)
        )

        # Temperature is assumed; preceding treatment history is unknown.
        self.parcels = deque([
            Parcel(self.capacity_l, initial, 0.0, False)
        ])

    def invalidate_history(self):
        """Call after missing data: prior exposure cannot be certified."""
        for parcel in self.parcels:
            parcel.history_complete = False

    def _displace(self, volume_l, inlet_c):
        """Remove from downstream end and insert at upstream end."""
        removed = []
        remaining = volume_l

        while remaining > 1e-12:
            parcel = self.parcels[0]
            amount = min(remaining, parcel.volume_l)
            removed.append(Parcel(
                amount,
                parcel.temperature_c,
                parcel.log_reduction,
                parcel.history_complete,
            ))

            parcel.volume_l -= amount
            remaining -= amount

            if parcel.volume_l <= 1e-12:
                self.parcels.popleft()

        if volume_l > 0:
            self.parcels.append(
                Parcel(volume_l, inlet_c, 0.0, True)
            )

        return removed

    def _expose(self, seconds, bath_c):
        factor = -math.expm1(-self.heating_rate * seconds)

        for parcel in self.parcels:
            before = parcel.temperature_c
            after = before + (bath_c - before) * factor

            exposure = profile_log_reduction(
                self.settings,
                [
                    {"travel_time_s": 0.0, "temperature_c": before},
                    {"travel_time_s": seconds, "temperature_c": after},
                ],
            )
            parcel.log_reduction += exposure["raw_model_log_reduction"]
            parcel.temperature_c = after

    def advance(self, duration_s, branch_flow_lpm, inlet_c, bath_c):
        """Advance one interval with piecewise-constant prescribed inputs.

        Flow is referenced to the configured constant density.
        Inputs must describe this interval, not just its endpoint.
        """
        duration = float(duration_s)
        flow = float(branch_flow_lpm)
        inlet = float(inlet_c)
        bath = float(bath_c)

        if any(not math.isfinite(x) for x in (duration, flow, inlet, bath)):
            raise ValueError("Tracking inputs must be finite")
        if duration <= 0 or flow < 0:
            raise ValueError("Positive duration and nonnegative flow required")

        # Limit movement to 5% of branch volume per substep.
        movement_limit = (
            math.inf if flow == 0
            else 0.05 * self.capacity_l / (flow / 60)
        )
        count = math.ceil(duration / min(self.max_step, movement_limit))
        if count > 100000:
            raise ValueError("Tracking interval requires too many substeps")

        dt = duration / count
        half_volume = flow * dt / 120
        discharge = []

        for _ in range(count):
            # Symmetric splitting: transport, exposure, transport.
            if flow > 0:
                discharge.extend(self._displace(half_volume, inlet))

            self._expose(dt, bath)

            if flow > 0:
                discharge.extend(self._displace(half_volume, inlet))

        inventory = sum(p.volume_l for p in self.parcels)
        if not math.isclose(inventory, self.capacity_l, abs_tol=1e-8):
            raise RuntimeError("Coil volume balance failed")

        return discharge


def summarize_discharge(parcels, target_log):
    """Interval-average outlet result; unknown-history portions invalidate it."""
    volume = sum(p.volume_l for p in parcels)
    if volume == 0:
        return {
            "discharged_volume_l": 0.0,
            "temperature_c": None,
            "history_complete": None,
            "raw_model_log_reduction": None,
            "model_target_reached": None,
        }

    temperature = sum(
        p.volume_l * p.temperature_c for p in parcels
    ) / volume
    complete = all(p.history_complete for p in parcels)

    reduction = None
    if complete:
        reduction = mix_log_reductions(
            [p.log_reduction for p in parcels],
            [p.volume_l / volume for p in parcels],
        )

    return {
        "discharged_volume_l": volume,
        "temperature_c": temperature,
        "history_complete": complete,
        "raw_model_log_reduction": reduction,
        "model_target_reached": (
            None if reduction is None else reduction >= target_log
        ),
    }