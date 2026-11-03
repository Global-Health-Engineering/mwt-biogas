"""Process state machine (control spec section 4).

Decides the plant state once per cycle from the cycle inputs and fault
flags. Treatment states are delegated to TreatmentLogic, which returns a
Transition; every transition is logged with its reason.
"""

from pasteurizer.controller.conditions import (
    fallback_entry_ok,
    model_entry_ok,
    start_permitted,
)
from pasteurizer.controller.treatment import Transition, TreatmentLogic
from pasteurizer.fault_manager import Persistence
from pasteurizer.modes import Mode
from pasteurizer.process_state import (
    ACTIVE_STATES,
    TREATMENT_STATES,
    ProcessState as P,
    TreatmentPath,
)


class ProcessStateMachine:
    def __init__(self, config, treatment: TreatmentLogic, faults, log, *, start_time):
        self.cfg = config
        self.cs = config.control
        self.treatment = treatment
        self.faults = faults
        self.log = log
        self.state = P.IDLE
        self.since = start_time
        self.reason = "Controller started"
        self.path: TreatmentPath | None = None
        self.stop_hold = False
        self.preheat_since: float | None = None
        self.dwell = Persistence()
        self.supply_lost = Persistence()

    # ------------------------------------------------------------------
    def go(self, transition: Transition, now: float):
        state = transition.state
        if transition.fault:
            self.faults.set(transition.fault, True)
        if state == self.state:
            return
        previous = self.state
        self._leave(previous, state, transition, now)
        self._enter(state, transition, now)
        self.log.record("STATE_TRANSITION", old=previous.value, new=state.value,
                        reason=transition.reason, interruption=transition.interruption)

    def _leave(self, previous, state, transition, now):
        if previous in TREATMENT_STATES and state not in TREATMENT_STATES:
            self.treatment.end()
            if transition.interruption:
                self.treatment.record_interruption(now)
        if previous == P.PREHEAT_3KW:
            self.preheat_since = None

    def _enter(self, state, transition, now):
        if state == P.PREHEAT_3KW:
            self.preheat_since = now
        if state in (P.IDLE, P.STOPPING, P.TRIPPED, P.PREHEAT_3KW):
            self.path = None
        if state not in ACTIVE_STATES:
            self.supply_lost.reset()
        if state in TREATMENT_STATES:
            self.treatment.begin(state, now, handover=transition.handover)
        self.dwell.reset()
        self.state = state
        self.since = now
        self.reason = transition.reason

    def _to(self, state, reason, now, **kwargs):
        self.go(Transition(state, reason, **kwargs), now)

    # ------------------------------------------------------------------
    def step(self, inputs, flags, last_written):
        now = inputs.now
        state = self.state
        if flags.summary.trip:
            self._to(P.TRIPPED, "Level 3 fault", now,
                     interruption=state in TREATMENT_STATES)
        elif state == P.TRIPPED:
            self._to(P.IDLE, "Level 3 faults reset", now)
        elif state in ACTIVE_STATES and self._must_stop(inputs):
            self._to(P.STOPPING, "GUI STOP" if self.stop_hold
                     else f"Mode {inputs.mode.value}", now)
        elif state == P.STOPPING:
            self._stopping(inputs, last_written)
        elif state == P.IDLE:
            self._idle(inputs, flags)
        else:
            self._active(inputs, flags, last_written)

    def _must_stop(self, inputs):
        return self.stop_hold or inputs.mode in (Mode.OFF, Mode.MANUAL)

    def _stopping(self, inputs, last_written):
        closed = inputs.y4_closed(self.cs.start.y4_closed_tolerance_percent)
        y3_closed = last_written is not None and not last_written.pasteurizer_isolation_open
        if closed and y3_closed:
            self._to(P.IDLE, "Shutdown complete", inputs.now)

    def _idle(self, inputs, flags):
        if not start_permitted(self.cfg, inputs, flags, stop_hold=self.stop_hold):
            return
        if inputs.pt5 < self.cfg.startup.cold_start_below_celsius:
            self._to(P.PREHEAT_3KW, "Cold start", inputs.now)
        else:
            self._to(P.HEAT_UP, "Warm start, preheat skipped", inputs.now)

    def _active(self, inputs, flags, last_written):
        now = inputs.now
        state = self.state
        if flags.heating_fault:
            self._to(P.IDLE, "PT5/PT6 invalid; heating off, startup checks repeat",
                     now, interruption=state in TREATMENT_STATES)
            return
        lost = self.supply_lost.update(inputs.supply is not True, now)
        if lost >= self.cs.treatment_stop.supply_wait_s:
            self._to(P.STOPPING, "Supply lost for too long", now)
            return
        if state == P.PREHEAT_3KW:
            self._preheat(inputs)
        elif state in TREATMENT_STATES:
            transition = self.treatment.step(state, inputs, flags, last_written)
            if transition is not None:
                self.go(transition, now)
        else:
            self._qualify(inputs, flags)

    def _preheat(self, inputs):
        complete = self.cfg.startup.preheat_complete_at_celsius
        if inputs.pt5 is not None and inputs.pt5 >= complete:
            self._to(P.HEAT_UP, "Preheat complete", inputs.now)

    # HEAT_UP / READY / FALLBACK_QUALIFY ----------------------------------
    def _select_path(self, inputs, flags):
        if inputs.model_ok:
            return TreatmentPath.MODEL
        if flags.fallback_ready:
            return TreatmentPath.FALLBACK
        return None

    def _qualify(self, inputs, flags):
        self.path = self._select_path(inputs, flags)
        if self.state == P.HEAT_UP:
            self._heat_up(inputs, flags)
        elif self.state == P.READY:
            self._ready(inputs, flags)
        else:
            self._fallback_qualify(inputs, flags)

    def _heat_up(self, inputs, flags):
        if self.path == TreatmentPath.MODEL and model_entry_ok(self.cs, inputs, flags):
            self._to(P.READY, "Model entry conditions met", inputs.now)
        elif self.path == TreatmentPath.FALLBACK:
            self._to(P.FALLBACK_QUALIFY, "Fallback path selected", inputs.now)

    def _ready(self, inputs, flags):
        now = inputs.now
        if self.path != TreatmentPath.MODEL or not model_entry_ok(self.cs, inputs, flags):
            self._to(P.HEAT_UP, "Model entry condition failed", now)
            return
        held = self.dwell.update(True, now)
        if held >= self.cs.model.ready_dwell_s and self.treatment.retry_wait_over(now):
            self._to(P.MODEL_TREATMENT, "Entry dwell held", now)

    def _fallback_qualify(self, inputs, flags):
        now = inputs.now
        if self.path == TreatmentPath.MODEL:
            self._to(P.HEAT_UP, "Model path usable again", now)
            return
        if self.path is None:
            self._to(P.HEAT_UP, "No treatment path available", now)
            return
        held = self.dwell.update(fallback_entry_ok(self.cs, inputs, flags), now)
        if held >= self.cs.fallback.dwell_s and self.treatment.retry_wait_over(now):
            self._to(P.FALLBACK_TREATMENT, "Fallback entry held", now)

    # Status helpers -------------------------------------------------------
    def dwell_remaining(self, now):
        if self.dwell.since is None:
            return None
        if self.state == P.READY:
            return max(0.0, self.cs.model.ready_dwell_s - (now - self.dwell.since))
        if self.state == P.FALLBACK_QUALIFY:
            return max(0.0, self.cs.fallback.dwell_s - (now - self.dwell.since))
        return None

    def supply_wait_remaining(self, now):
        if self.supply_lost.since is None:
            return None
        return max(0.0, self.cs.treatment_stop.supply_wait_s
                   - (now - self.supply_lost.since))
