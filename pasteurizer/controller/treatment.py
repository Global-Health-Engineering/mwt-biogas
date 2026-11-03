"""Treatment run logic (control spec sections 7-9).

Owns everything that only exists while treating: release detection, the Y4
controllers, stop criteria, the single low-flow retry and the interruption
count. `step` never changes the plant state itself; it returns a Transition
for the state machine to apply.
"""

from collections import deque
from dataclasses import dataclass, field

from pasteurizer.controller.conditions import model_return_ok
from pasteurizer.fault_manager import Persistence
from pasteurizer.modes import Mode
from pasteurizer.process_state import ProcessState as P
from pasteurizer.treatment_control import FallbackStepController, FlowPIController


@dataclass(frozen=True)
class Transition:
    state: P
    reason: str
    interruption: bool = False      # counts toward the repeated-interruption warning
    handover: bool = False          # planned model/fallback handover, Y4 continuous
    fault: str | None = None        # latch this fault together with the transition


@dataclass
class TreatmentRun:
    started_at: float
    released_at: float | None = None
    good_flow_since: float | None = None
    pt3_low: Persistence = field(default_factory=Persistence)
    return_check: Persistence = field(default_factory=Persistence)
    grace_passed: bool = False


class TreatmentLogic:
    def __init__(self, config, log):
        self.cs = config.control
        self.log = log
        self.step_controller = FallbackStepController(self.cs.fallback)
        self.flow_pi = (FlowPIController(self.cs.flow_pi)
                        if self.cs.flow_pi.assigned else None)
        self.run: TreatmentRun | None = None
        self.y4_command = 0.0
        self.retry_exhausted = False
        self.retry_pending = False
        self.retry_wait_until: float | None = None
        self.high_flow_pending = False
        self._interruptions = deque()

    # ------------------------------------------------------------------
    # Bookkeeping used by the state machine and fault evaluation
    # ------------------------------------------------------------------

    def begin(self, state, now, *, handover=False):
        self.high_flow_pending = False
        if handover:
            self.run.return_check.reset()
            controller = (self.flow_pi if state == P.MODEL_TREATMENT else None) \
                or self.step_controller
            controller.start(now, self.y4_command)
            return
        self.run = TreatmentRun(started_at=now)
        self.y4_command = self.cs.fallback.start_opening_percent

    def end(self):
        self.run = None
        self.y4_command = 0.0

    def record_interruption(self, now):
        self._interruptions.append(now)

    def interruption_count(self, now) -> int:
        window = self.cs.treatment_stop.interruption_window_s
        while self._interruptions and now - self._interruptions[0] > window:
            self._interruptions.popleft()
        return len(self._interruptions)

    def retry_wait_over(self, now) -> bool:
        return self.retry_wait_until is None or now >= self.retry_wait_until

    def restore_retry(self):
        self.retry_exhausted = False

    # ------------------------------------------------------------------
    # One treatment cycle
    # ------------------------------------------------------------------

    def step(self, state, inputs, flags, last_written) -> Transition | None:
        now = inputs.now
        self._detect_release(state, now, last_written)
        waiting = P.FALLBACK_QUALIFY if state == P.FALLBACK_TREATMENT else P.HEAT_UP

        for check in (self._supply, self._availability, self._sensor_faults,
                      self._under_temperature, self._low_flow):
            transition = check(state, inputs, flags, waiting)
            if transition is not None:
                return transition

        self._track_good_flow(inputs)
        if state == P.FALLBACK_TREATMENT:
            return self._fallback(inputs)
        return self._model(inputs, flags)

    def _detect_release(self, state, now, last_written):
        run = self.run
        if run.released_at is not None or last_written is None:
            return
        if last_written.control_valve_percent <= 0:
            return
        run.released_at = now
        opening = last_written.control_valve_percent
        if state == P.MODEL_TREATMENT and self.flow_pi is not None:
            self.flow_pi.start(now, opening)
        else:
            self.step_controller.start(now, opening)

    def _supply(self, state, inputs, flags, waiting):
        # Confirmed supply loss takes precedence over low flow in the same scan.
        if inputs.supply is True:
            return None
        target = P.READY if state == P.MODEL_TREATMENT else P.FALLBACK_QUALIFY
        return Transition(target, "Supply lost; heating continues")

    def _availability(self, state, inputs, flags, waiting):
        if inputs.mode == Mode.AUTO and inputs.acquisition_ok:
            return None
        return Transition(waiting, "Selector or acquisition unavailable")

    def _sensor_faults(self, state, inputs, flags, waiting):
        if not flags.treatment_fault:
            return None
        return Transition(waiting, "Treatment sensor or Y4 feedback invalid",
                          interruption=True)

    def _under_temperature(self, state, inputs, flags, waiting):
        stop = self.cs.treatment_stop
        held = self.run.pt3_low.update(inputs.pt3 < stop.pt3_min_c, inputs.now)
        if self.run.released_at is None or held < stop.pt3_confirm_s:
            return None
        return Transition(P.HEAT_UP, f"PT3 below {stop.pt3_min_c} C", interruption=True)

    def _after_grace(self, now) -> bool:
        stop = self.cs.treatment_stop
        grace = stop.retry_grace_s if self.retry_pending else stop.startup_grace_s
        return self.run.released_at is not None and now - self.run.released_at >= grace

    def _low_flow(self, state, inputs, flags, waiting):
        stop = self.cs.treatment_stop
        if not self._after_grace(inputs.now):
            return None
        if inputs.flow3 >= stop.min_flow_l_min:
            if not self.run.grace_passed:
                self.run.grace_passed = True
                self.retry_pending = False
            return None
        if not self.retry_exhausted:
            self.retry_exhausted = True
            self.retry_pending = True
            self.retry_wait_until = inputs.now + stop.retry_wait_s
            return Transition(waiting, "Low treatment flow; one retry", interruption=True)
        self.retry_pending = False
        return Transition(P.TRIPPED, "Low treatment flow after retry", interruption=True,
                          fault="LOW_TREATMENT_FLOW_RETRY_FAILED")

    def _track_good_flow(self, inputs):
        run = self.run
        if not run.grace_passed:
            return
        if run.good_flow_since is None:
            run.good_flow_since = inputs.now
        restore_after = self.cs.treatment_stop.retry_restore_s
        if self.retry_exhausted and inputs.now - run.good_flow_since >= restore_after:
            self.retry_exhausted = False
            self.log.record("LOW_FLOW_RETRY_RESTORED")

    def _fallback(self, inputs):
        run = self.run
        limit = self.cs.fallback.max_flow_l_min
        if run.released_at is not None and inputs.flow3 > limit:
            self.high_flow_pending = True
            return Transition(P.FALLBACK_QUALIFY, "Flow above fallback maximum",
                              interruption=True)
        held = run.return_check.update(model_return_ok(inputs), inputs.now)
        if held >= self.cs.model.return_check_s:
            return Transition(P.MODEL_TREATMENT, "Return check held", handover=True)
        if run.released_at is not None:
            self.y4_command = self.step_controller.update(inputs.pt3, inputs.flow3,
                                                          inputs.now)
        return None

    def _model(self, inputs, flags):
        stop = self.cs.treatment_stop
        result = inputs.model
        if not inputs.model_ok:
            if flags.fallback_ready:
                return Transition(P.FALLBACK_QUALIFY, "Model unusable; fallback",
                                  interruption=True)
            return Transition(P.HEAT_UP, "Model unusable; no fallback", interruption=True)
        if result.achievable_flow_l_min < stop.min_flow_l_min:
            return Transition(P.HEAT_UP, "Achievable flow below minimum", interruption=True)
        if result.predicted_log_reduction < self.cs.model.target_log:
            return Transition(P.HEAT_UP, "Predicted reduction below target",
                              interruption=True)
        if self.run.released_at is not None and self.flow_pi is not None:
            self.y4_command = self.flow_pi.update(result.achievable_flow_l_min,
                                                  inputs.flow3, inputs.now)
        return None
