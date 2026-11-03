"""Supervision of control valve Y4 (control spec section 12).

- Position: feedback must reach each successfully written target within
  the movement timeout.
- Feedback availability: while Y4 matters (plant active, stopping, or Y4
  commanded open) missing feedback inhibits release at once; it escalates
  to a pasteurizer Level 3 if it stays missing for the movement timeout.
- Analogue output status fault.
"""

from dataclasses import dataclass

from pasteurizer.valves import ValveSupervisor


@dataclass(frozen=True)
class Y4Check:
    feedback_missing: bool | None   # None while acquisition is unavailable
    position_timeout: bool
    output_fault: bool
    healthy: bool                   # feedback valid and at its target


class Y4Monitor:
    def __init__(self, valve_settings):
        self.supervisor = ValveSupervisor(valve_settings.position_tolerance_percent,
                                          valve_settings.movement_timeout_seconds)
        self.timeout_s = valve_settings.movement_timeout_seconds

    def command_written(self, percent: float, now: float):
        """Call only after the write succeeded."""
        self.supervisor.set_target(percent, now)

    @property
    def commanded_open(self) -> bool:
        target = self.supervisor.target
        return target is not None and target > 0

    def check(self, inputs, *, relevant: bool) -> Y4Check:
        feedback = inputs.y4_feedback
        if not inputs.acquisition_ok:
            return Y4Check(None, False, False, False)
        status = self.supervisor.update(feedback, inputs.now)
        return Y4Check(
            feedback_missing=(relevant or self.commanded_open) and not feedback.valid,
            position_timeout=status == "POSITION_TIMEOUT",
            output_fault=not inputs.y4_output_healthy,
            healthy=feedback.valid and status in ("AT_TARGET", "NO_TARGET"),
        )
