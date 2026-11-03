"""Transfer-pump flow supervision, one instance per line (spec section 10).

An NPN sensor that gives no pulses cannot tell zero flow from a sensor or
wiring failure, so the no-flow fault only names the symptom: pump commanded
ON without measured flow. An unavailable measurement (acquisition, counter
timing or continuity) is a separate condition from a valid measured zero.
"""

from dataclasses import dataclass

from pasteurizer.contracts import FlowSignal
from pasteurizer.fault_manager import Persistence

# A flow value older than this is not fresh (flows are evaluated about 1 /s).
MAX_FLOW_AGE_S = 2.5


@dataclass(frozen=True)
class TransferFlowStatus:
    measurement_invalid: bool | None   # requested and measurement unavailable
    no_flow: bool | None               # running, after grace, <= no-flow limit held
    low_flow: bool | None              # running, after grace, low positive flow held


def measured_flow(flow: FlowSignal | None, now: float) -> float | None:
    """Fresh, valid, calibrated flow in L/min, else None."""
    if flow is None or not flow.valid or flow.flow_lpm is None:
        return None
    if flow.sampled_at is None or now - flow.sampled_at > MAX_FLOW_AGE_S:
        return None
    return flow.flow_lpm


class TransferFlowSupervisor:
    def __init__(self, settings):
        self.s = settings
        self.reset()

    def reset(self):
        self._running_since = None
        self._no_flow = Persistence()
        self._low_flow = Persistence()

    def update(self, requested: bool, running: bool, flow, now: float) -> TransferFlowStatus:
        """requested: equipment logic wants the pump on; running: written ON."""
        value = measured_flow(flow, now)
        invalid = (value is None) if requested or running else False

        if not running:
            self.reset()
            return TransferFlowStatus(invalid, False, False)

        if self._running_since is None:
            self._running_since = now
        in_grace = now - self._running_since < self.s.grace_s

        if value is None or in_grace:
            # Unknown or still starting: timers neither run nor reset.
            no_flow = low_flow = None if value is None else False
            if in_grace:
                self._no_flow.reset()
                self._low_flow.reset()
            return TransferFlowStatus(invalid, no_flow, low_flow)

        held_no = self._no_flow.update(value <= self.s.no_flow_max_l_min, now)
        held_low = self._low_flow.update(
            self.s.no_flow_max_l_min < value < self.s.low_flow_below_l_min, now)
        return TransferFlowStatus(
            invalid,
            held_no >= self.s.persistence_s,
            held_low >= self.s.persistence_s,
        )
