"""Parcel tracking with periodically updated representative branch U."""

import math

from .tracking import CoilTracker
from .transient_convection import branch_coefficients
from .convection import liquid_water


class VariableCoilTracker(CoilTracker):
    def __init__(self, settings, branch_index):
        super().__init__(settings, branch_index)
        self.branch_index = branch_index

        options = settings["tracking"]
        self.update_interval = float(
            options["coefficient_update_interval_s"]
        )
        self.equal_band = float(
            options["coefficient_equal_temperature_band_c"]
        )

        if (
            not math.isfinite(self.update_interval)
            or self.update_interval <= 0
            or not math.isfinite(self.equal_band)
            or self.equal_band < 1e-5
        ):
            raise ValueError("Invalid coefficient-update settings")

        self.latest_coefficients = None
        self.current_flags = []
        self.encountered_flags = set()
        
        self.coefficient_remaining_s = 0.0
        self.previous_flowing = None

    def _update_coefficient(self, flow, bath):
        bulk = sum(
            parcel.volume_l * parcel.temperature_c
            for parcel in self.parcels
        ) / self.capacity_l

        if flow == 0:
            self.current_flags = [
                "Stopped flow: previous or configured U retained provisionally"
            ]
            self.latest_coefficients = None

        elif abs(bath - bulk) <= self.equal_band:
            self.current_flags = [
                "Near-equal mean/bath temperatures: previous U retained"
            ]
            self.latest_coefficients = None

        else:
            result = branch_coefficients(
                self.settings,
                self.branch_index,
                flow,
                bulk,
                bath,
            )
            new_u = result["overall_u_outer_w_m2k"]

            # Same geometry and constant reference properties:
            # heating_rate is directly proportional to U.
            geometry = self.settings["geometry"]
            outer = float(geometry["tube_outer_diameter_m"])
            bore = outer - 2 * float(geometry["wall_thickness_m"])

            self.u = new_u
            self.heating_rate = (
                4 * outer * new_u
                / (self.density * self.cp * bore**2)
            )
            self.latest_coefficients = result
            self.current_flags = list(result["flags"])

        self.encountered_flags.update(self.current_flags)

    def advance(self, duration_s, branch_flow_lpm, inlet_c, bath_c):
        duration = float(duration_s)
        flow = float(branch_flow_lpm)
        inlet = float(inlet_c)
        bath = float(bath_c)

        if any(not math.isfinite(x) for x in (duration, flow, inlet, bath)):
            raise ValueError("Tracking inputs must be finite")
        if duration <= 0 or flow < 0:
            raise ValueError("Positive duration and nonnegative flow required")

        pressure = self.settings["heat_transfer"]["property_pressure_mpa"]
        liquid_water(inlet, pressure)
        liquid_water(bath, pressure)

        flowing = flow > 0
        remaining = duration
        discharge = []

        try:
            # Startup, stopping and restarting require an immediate update.
            if (
                self.previous_flowing is None
                or flowing != self.previous_flowing
            ):
                self.coefficient_remaining_s = 0.0

            while remaining > 0.0:
                if self.coefficient_remaining_s <= 1e-9:
                    self._update_coefficient(flow, bath)
                    self.coefficient_remaining_s = self.update_interval

                interval = min(
                    remaining,
                    self.coefficient_remaining_s,
                )

                discharge.extend(
                    super().advance(interval, flow, inlet, bath)
                )

                remaining = max(0.0, remaining - interval)
                self.coefficient_remaining_s = max(
                    0.0,
                    self.coefficient_remaining_s - interval,
                )

            self.previous_flowing = flowing

        except Exception:
            self.invalidate_history()
            self.coefficient_remaining_s = 0.0
            self.previous_flowing = None
            raise

        return discharge