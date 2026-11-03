"""Y4 controllers for treatment (spec sections 7 and 8).

Both controllers share one interface (start, update, command), so the
fallback step controller can later be replaced by a PT3 PI, and control can
be handed between model and fallback without a jump: the incoming
controller starts from Y4's current accepted command.
"""

from pasteurizer.numbers import is_finite_number


class FallbackStepController:
    """PT3 step controller with a hold band and a measured-flow limit."""

    def __init__(self, settings):
        self.s = settings
        self.command = 0.0
        self._last_step = None

    def start(self, now: float, opening: float | None = None):
        start = self.s.start_opening_percent if opening is None else opening
        self.command = min(max(start, self.s.min_opening_percent),
                           self.s.max_opening_percent)
        self._last_step = now

    def update(self, pt3_c, flow_l_min, now: float) -> float:
        s = self.s
        if self._last_step is None:
            self.start(now)
        if now - self._last_step < s.step_interval_s:
            return self.command
        self._last_step = now
        if not is_finite_number(pt3_c):
            return self.command
        if pt3_c < s.pt3_target_c - s.hold_band_c:
            self.command -= s.step_percent
        elif pt3_c > s.pt3_target_c + s.hold_band_c:
            at_limit = (s.max_flow_l_min is not None and is_finite_number(flow_l_min)
                        and flow_l_min >= s.max_flow_l_min)
            if not at_limit:
                self.command += s.step_percent
        self.command = min(max(self.command, s.min_opening_percent),
                           s.max_opening_percent)
        return self.command


class FlowPIController:
    """Flow PI: measured flow sensor 3 follows the model's flow request."""

    def __init__(self, settings, valve_min=0.0, valve_max=100.0):
        if not settings.assigned:
            raise ValueError("Flow PI tuning values are UNASSIGNED")
        self.s = settings
        self.valve_min = valve_min
        self.valve_max = valve_max
        self.command = 0.0
        self._integral = 0.0
        self._setpoint = None
        self._last = None

    def start(self, now: float, opening: float):
        self.command = min(max(opening, self.valve_min), self.valve_max)
        self._integral = self.command   # bumpless: output equals current command
        self._setpoint = None
        self._last = now

    def update(self, setpoint_l_min, measured_l_min, now: float) -> float:
        s = self.s
        if self._last is None:
            self.start(now, self.command)
        dt = max(0.0, now - self._last)
        self._last = now
        if not (is_finite_number(setpoint_l_min) and is_finite_number(measured_l_min)) or dt == 0.0:
            return self.command

        target = min(setpoint_l_min, s.max_flow_l_min)
        if self._setpoint is None:
            self._setpoint = measured_l_min
        step = s.flow_rate_limit_l_min_per_s * dt
        self._setpoint += max(-step, min(step, target - self._setpoint))

        error = self._setpoint - measured_l_min
        candidate = self._integral + s.ki_percent_per_l_min_s * error * dt
        # Anti-windup: integrate only while the output is not saturated further.
        raw = s.kp_percent_per_l_min * error + candidate
        if self.valve_min <= raw <= self.valve_max:
            self._integral = candidate
        raw = s.kp_percent_per_l_min * error + self._integral

        slew = s.slew_percent_per_s * dt
        raw = self.command + max(-slew, min(slew, raw - self.command))
        self.command = min(max(raw, self.valve_min), self.valve_max)
        return self.command
