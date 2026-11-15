"""Process state machine (control spec section 4).

Decides the plant state once per cycle from the cycle inputs and fault
flags. A cold start without solar permission for the 3 kW heater waits in
WAITING_FOR_SOLAR (nothing heats) and preheats once the permission is there;
losing the permission during preheat returns to WAITING_FOR_SOLAR. The
preheat heating time is kept across these changes. Treatment states are
delegated to TreatmentLogic, which returns a Transition; every transition is
logged with its reason.
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

# One cold start: waiting for solar and preheating share the heating time.
PREHEAT_STATES = (P.WAITING_FOR_SOLAR, P.PREHEAT_3KW)


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
        # Time with the 3 kW heater written on in this cold start (PREHEAT_3KW,
        # also across WAITING_FOR_SOLAR). The preheat timeout counts this only.
        self.preheat_heated_s = 0.0
        self._preheat_tick: float | None = None
        self._solar_3kw = (True, "")
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
        if previous in PREHEAT_STATES and state not in PREHEAT_STATES:
            self.preheat_since = None           # this cold start is over

    def _enter(self, state, transition, now):
        if state in PREHEAT_STATES and self.preheat_since is None:
            self.preheat_since = now            # new cold start: heating time from 0
            self.preheat_heated_s = 0.0
        if state == P.PREHEAT_3KW:
            self._preheat_tick = now
        if state in (P.IDLE, P.STOPPING, P.TRIPPED) or state in PREHEAT_STATES:
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
    def _count_preheat(self, now, last_written):
        """Add the time since the last step when the 3 kW was written on."""
        if self.state == P.PREHEAT_3KW and self._preheat_tick is not None:
            if last_written is not None and last_written.heater_3kw:
                self.preheat_heated_s += max(0.0, now - self._preheat_tick)
        self._preheat_tick = now

    def step(self, inputs, flags, last_written, *, solar_3kw=(True, "")):
        """solar_3kw: (permitted, reason) of the solar permission for the 3 kW
        heater in this cycle (energy.py; always permitted when the rule is off)."""
        self._solar_3kw = solar_3kw
        now = inputs.now
        self._count_preheat(now, last_written)
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
            permitted, reason = self._solar_3kw
            if permitted:
                self._to(P.PREHEAT_3KW, "Cold start", inputs.now)
            else:
                self._to(P.WAITING_FOR_SOLAR,
                         f"Cold start; not enough solar power for the 3 kW heater: {reason}",
                         inputs.now)
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
        if state in PREHEAT_STATES:
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
            return
        permitted, reason = self._solar_3kw
        if self.state == P.WAITING_FOR_SOLAR and permitted:
            self._to(P.PREHEAT_3KW, "Solar power available for the 3 kW heater", inputs.now)
        elif self.state == P.PREHEAT_3KW and not permitted:
            self._to(P.WAITING_FOR_SOLAR,
                     f"Not enough solar power for the 3 kW heater: {reason}", inputs.now)

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
