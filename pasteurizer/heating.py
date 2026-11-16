"""Heating behaviours below the plant state machine (spec section 6).

- AssistHeater: 1 kW hysteresis with minimum switching times.
- OutputTimer: on/off times of an output, taken from the written frames.
- PreheatCirculation: heating-water circulation pulses in a cold start.
- BiogasHeatMonitor: diagnostic for biogas heat input (never blocks).

The 3 kW stage and continuous circulation in the second stage follow the
plant state directly. There is no pump running, burner enable or flame
feedback: "on" always means "written on".
"""

from dataclasses import asdict, dataclass
import math

from pasteurizer.numbers import is_finite_number


class AssistHeater:
    def __init__(self, settings):
        self.settings = settings
        self.reset()

    def reset(self):
        self.on = False
        self.on_since = None
        self.off_since = None

    def update(self, pt5_c, on_below_c, off_at_c, permitted: bool, now: float) -> bool:
        """Return the 1 kW request.

        Withdrawn permission (stop, critical fault, invalid temperature,
        overtemperature) switches off at once, overriding the minimum ON time.
        """
        valid = (isinstance(pt5_c, (int, float)) and not isinstance(pt5_c, bool)
                 and math.isfinite(pt5_c))
        if not permitted or not valid:
            if self.on:
                self.on = False
                self.off_since = now
            return False

        s = self.settings
        if self.on:
            if pt5_c >= off_at_c and now - self.on_since >= s.min_on_s:
                self.on = False
                self.off_since = now
        else:
            off_long_enough = self.off_since is None or now - self.off_since >= s.min_off_s
            if pt5_c < on_below_c and off_long_enough:
                self.on = True
                self.on_since = now
        return self.on


class OutputTimer:
    """How long an output has been written on or off (no physical feedback).

    Updated once per cycle from the last written frame; an unknown frame
    (None: write failed or not yet written) counts as off, so it never
    credits running time.
    """

    def __init__(self):
        self.on_since: float | None = None
        self.off_since: float | None = None
        # Last off -> on edge (for the circulation: the last burner ignition
        # call). Kept across plant states; unknown after a controller restart.
        self.started_at: float | None = None

    def update(self, on: bool, now: float):
        if on:
            if self.on_since is None:
                self.on_since = now
                self.started_at = now
            self.off_since = None
        else:
            if self.off_since is None:
                self.off_since = now
            self.on_since = None

    @property
    def on(self) -> bool:
        return self.on_since is not None

    def on_for(self, now: float) -> float:
        return 0.0 if self.on_since is None else now - self.on_since

    def off_for(self, now: float) -> float:
        return 0.0 if self.off_since is None else now - self.off_since

    def since_start(self, now: float) -> float | None:
        return None if self.started_at is None else now - self.started_at


def _delta(pt5, pt6):
    """PT6 - PT5 from valid readings, else None."""
    if not (is_finite_number(pt5) and is_finite_number(pt6)):
        return None
    return pt6 - pt5


class PreheatCirculation:
    """Circulation pulses during a cold start (PREHEAT_3KW and WAITING_FOR_SOLAR).

    Every pulse start calls for burner ignition, so every start - whatever
    its trigger, including a restart after an interruption - needs both
      - min_off_s of written-off time since the last pulse, and
      - min_start_interval_s since the last written pump start.

    Triggers (judged only when both limits allow a start):
      PT5_CHECK  valid PT5 at the preheat limit read with the pump off: a
                 pulse refreshes it before HEAT_UP may be entered (both states)
      DELTA      valid PT6 - PT5 >= trigger_delta_c (PREHEAT_3KW only)
      MAX_IDLE   no circulation for max_idle_s, counted from the later of the
                 last pulse and the start of this cold start (PREHEAT_3KW only)

    3 kW pause (PREHEAT_3KW): a valid PT5 at the preheat limit that is not a
    refreshed reading (pump written on < min_run_s) latches a pause of the
    3 kW until a refresh pulse has run min_run_s. Then either the plant goes
    to HEAT_UP (mixed PT5 still at the limit) or, with the mixed PT5 below
    it, the latch clears and the 3 kW resumes. The pause is not applied
    while the solar probe for the 3 kW runs (switching off would fail it).

    A pulse lasts min_run_s of written-on time. It finishes when the state
    changes between PREHEAT_3KW and WAITING_FOR_SOLAR (a solar loss is not a
    safety stop; cutting it would make a short burner cycle). A withdrawn
    heating permission (trip, STOP, invalid temperature, leaving the cold
    start) ends it at once, overriding the minimum run time.
    """

    def __init__(self, settings, preheat_complete_c: float):
        self.settings = settings
        self.complete_c = preheat_complete_c
        self.reset()

    def reset(self):
        self.requested = False
        self.trigger: str | None = None     # PT5_CHECK, DELTA or MAX_IDLE while a pulse runs
        self.since: float | None = None     # start of this cold start (first update)
        self.hold_3kw = False               # PT5 at the limit, waiting for a mixed reading
        self.reason = "Not in a cold start"

    def pause_3kw(self, probing: bool) -> bool:
        """Hold the 3 kW off now (never during its solar probe)."""
        return self.hold_3kw and not probing

    def start_wait(self, pump: OutputTimer, now: float) -> float:
        """Seconds until the start limits (minimum OFF, start interval) allow a pulse."""
        s = self.settings
        if pump.on:
            return s.min_off_s
        wait = max(0.0, s.min_off_s - pump.off_for(now))
        since = pump.since_start(now)
        if since is not None:
            wait = max(wait, s.min_start_interval_s - since)
        return wait

    def update(self, pt5, pt6, *, permitted: bool, heating: bool, pump: OutputTimer,
               now: float) -> bool:
        """heating: PREHEAT_3KW (all triggers); False: WAITING_FOR_SOLAR (PT5 check only)."""
        s = self.settings
        if self.since is None:
            self.since = now
        delta = _delta(pt5, pt6)
        if not permitted:
            self.requested, self.trigger = False, None
            self.reason = "Heating not permitted: circulation off"
            return False
        at_limit = is_finite_number(pt5) and pt5 >= self.complete_c
        refreshed = pump.on and pump.on_for(now) >= s.min_run_s
        if at_limit and not refreshed:
            self.hold_3kw = True
        elif refreshed and not at_limit:
            self.hold_3kw = False           # mixed PT5 below the limit: preheat resumes
        if self.requested:
            ran = pump.on_for(now)
            if pump.on and ran >= s.min_run_s:
                self.requested, self.trigger = False, None
                self.reason = f"Pulse complete after {s.min_run_s:g} s"
            else:
                self.reason = (f"Pulse ({self._trigger_text()}): "
                               f"{max(0.0, s.min_run_s - ran):.0f} s left"
                               if pump.on else
                               f"Pulse ({self._trigger_text()}): waiting for the pump output")
            return self.requested
        if pump.on:
            self.reason = "Pulse ended: waiting for the pump output to be off"
            return False
        idle = min(pump.off_for(now), now - self.since)
        stagnant_hot = is_finite_number(pt5) and pt5 >= self.complete_c
        if stagnant_hot:
            trigger = "PT5_CHECK"
        elif heating and delta is not None and delta >= s.trigger_delta_c:
            trigger = "DELTA"
        elif heating and idle >= s.max_idle_s:
            trigger = "MAX_IDLE"
        else:
            trigger = None
        wait = self.start_wait(pump, now)
        if trigger is not None and wait > 0:
            self.reason = (f"{self._trigger_text(delta, trigger)}: pulse start held "
                           f"{wait:.0f} s (minimum OFF {s.min_off_s:g} s, start interval "
                           f"{s.min_start_interval_s:g} s)")
            return False
        if trigger is None:
            shown = "invalid" if delta is None else f"{delta:.1f} °C"
            self.reason = (f"Off: PT6 - PT5 {shown} (pulse at {s.trigger_delta_c:g} °C), "
                           f"mixing pulse in {max(0.0, s.max_idle_s - idle):.0f} s"
                           if heating else "Off: waiting for solar (PT5 below the preheat limit)")
            return False
        self.requested, self.trigger = True, trigger
        self.reason = f"Pulse started ({self._trigger_text(delta)})"
        return True

    def _trigger_text(self, delta=None, trigger=None) -> str:
        trigger = trigger or self.trigger
        if trigger == "PT5_CHECK":
            return f"PT5 at {self.complete_c:g} °C read without circulation: refresh check"
        if trigger == "DELTA":
            shown = "" if delta is None else f" {delta:.1f} °C"
            return f"PT6 - PT5{shown} >= {self.settings.trigger_delta_c:g} °C"
        return f"no circulation for {self.settings.max_idle_s:g} s"

    def status(self, pump: OutputTimer, now: float) -> dict:
        return {"requested": self.requested, "trigger": self.trigger,
                "heater_3kw_hold": self.hold_3kw,
                "pump_on_s": round(pump.on_for(now), 1) if pump.on else None,
                "pump_off_s": round(pump.off_for(now), 1) if not pump.on else None,
                "next_start_allowed_in_s": round(self.start_wait(pump, now), 1)
                if not pump.on else None,
                "reason": self.reason}


CHECKING = "CHECKING"
DETECTED = "DETECTED"
NOT_DETECTED = "NOT_DETECTED"
LABELS = {CHECKING: "Checking", DETECTED: "Biogas heat detected",
          NOT_DETECTED: "Biogas heat not detected"}


@dataclass(frozen=True)
class BiogasHeatStatus:
    status: str | None           # CHECKING / DETECTED / NOT_DETECTED; None: not evaluated
    label: str
    warning: bool | None         # True raise, False clear, None keep (fault manager)
    delta_c: float | None        # PT6 - PT5 from valid readings
    circulation_s: float | None  # written-on time of the circulation
    window_remaining_s: float | None   # initial check still running
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class BiogasHeatMonitor:
    """Is the burner delivering heat? Diagnostic only, never an interlock.

    Evaluated while AUTO circulates the heating water continuously (second
    stage). After startup_delay_s of written-on circulation, PT6 - PT5 >=
    delta_min_c held for hold_s reports DETECTED. Without that within
    observation_s of circulation, or when a detected heat input stays below
    the threshold for loss_s, it reports NOT_DETECTED and raises the warning;
    the warning clears when heat qualifies again. Invalid readings never
    qualify and never count as a loss. Circulation stopping resets the
    qualification (status CHECKING, warning kept); leaving the second stage
    resets everything and clears the warning.
    """

    def __init__(self, settings):
        self.settings = settings
        self.reset()

    def reset(self):
        self.status: str | None = None
        self._run_start: float | None = None
        self._above_since: float | None = None
        self._below_since: float | None = None
        self._window_open = False

    def update(self, *, active: bool, pump: OutputTimer, pt5, pt6, now: float) -> BiogasHeatStatus:
        s = self.settings
        delta = _delta(pt5, pt6)
        if not active:
            self.reset()
            return self._result(False, delta, None, None, "Not circulating in AUTO heat-up")
        if not pump.on:
            if self.status != NOT_DETECTED:
                self.status = CHECKING
            self._run_start = self._above_since = self._below_since = None
            return self._result(None, delta, None, None,
                                "Circulation not written on: qualification reset")
        if self._run_start != pump.on_since:       # a new circulation run
            self._run_start = pump.on_since
            self._above_since = self._below_since = None
            self._window_open = True
            if self.status != NOT_DETECTED:
                self.status = CHECKING
        run = now - self._run_start
        window = (max(0.0, s.observation_s - run)
                  if self._window_open and self.status == CHECKING else None)
        if run < s.startup_delay_s:
            return self._result(None, delta, run, window,
                                f"Startup delay: {s.startup_delay_s - run:.0f} s left")

        above = delta is not None and delta >= s.delta_min_c
        below = delta is not None and delta < s.delta_min_c
        if not above:
            self._above_since = None
        elif self._above_since is None:
            self._above_since = now
        if below:
            self._below_since = self._below_since if self._below_since is not None else now
        elif above:
            self._below_since = None
        # invalid reading: loss timer kept, qualification reset

        held = 0.0 if self._above_since is None else now - self._above_since
        shown = "invalid" if delta is None else f"{delta:.1f} °C"
        if self.status in (CHECKING, NOT_DETECTED):
            if above and held >= s.hold_s:
                self.status = DETECTED
                self._window_open = False
                self._below_since = None
                return self._result(False, delta, run, None,
                                    f"PT6 - PT5 {shown} >= {s.delta_min_c:g} °C for {s.hold_s:g} s")
            if self.status == CHECKING and run >= s.observation_s:
                self.status = NOT_DETECTED
                self._window_open = False
                return self._result(True, delta, run, None,
                                    f"No heat input within {s.observation_s:g} s of circulation")
            return self._result(None, delta, run, window,
                                f"PT6 - PT5 {shown}; needs >= {s.delta_min_c:g} °C "
                                f"for {s.hold_s:g} s (held {held:.0f} s)")
        # DETECTED: keep watching for a sustained loss.
        lost = 0.0 if self._below_since is None else now - self._below_since
        if lost >= s.loss_s:
            self.status = NOT_DETECTED
            self._above_since = None
            return self._result(True, delta, run, None,
                                f"PT6 - PT5 below {s.delta_min_c:g} °C for {s.loss_s:g} s")
        return self._result(None, delta, run, None,
                            f"PT6 - PT5 {shown}" + (f"; below threshold {lost:.0f} s"
                                                    if lost else ""))

    def _result(self, warning, delta, run, window, reason) -> BiogasHeatStatus:
        return BiogasHeatStatus(
            status=self.status, label=LABELS.get(self.status, "Not evaluated"),
            warning=warning,
            delta_c=None if delta is None else round(delta, 2),
            circulation_s=None if run is None else round(run, 1),
            window_remaining_s=None if window is None else round(window, 1),
            reason=reason)
