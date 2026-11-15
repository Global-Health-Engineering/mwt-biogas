"""Signal events for the event log: what the inputs did, on change only.

Logging only: nothing here feeds back into control. Called once per cycle,
right after the input stage, so these events come before the output events
of the same cycle (every event carries the cycle number, see event_log.py).

  LEVEL_CHANGED   a debounced level switch changed (actual reading, not the
                  override); `initial` marks the first known value of a run
  SENSOR_VALIDITY a sensor became invalid or valid again, for every sensor
                  and in every plant state (the alarms only cover sensors
                  where they matter). The change must persist
                  `signal_validity_hold_s`, so restarts and short dropouts
                  stay out of the log. Valid sensors at startup are not logged.
  FLOW_STARTED    a flow sensor registers flow (above `flow_activity_min_l_min`,
                  or `flow_activity_min_pulse_hz` without a K-factor) for
                  `flow_activity_hold_s`
  FLOW_STOPPED    it stopped registering flow for that long; with duration,
                  volume, average and peak flow, and the cause

While acquisition is unavailable no signal events are produced (the
ACQUISITION alarms cover that); afterwards only real differences are logged.
"""

LEVEL_CHANGED = "LEVEL_CHANGED"
SENSOR_VALIDITY = "SENSOR_VALIDITY"
FLOW_STARTED = "FLOW_STARTED"
FLOW_STOPPED = "FLOW_STOPPED"
SIGNAL_KINDS = (LEVEL_CHANGED, SENSOR_VALIDITY, FLOW_STARTED, FLOW_STOPPED)

Y4_FEEDBACK = "y4_feedback"


class _Held:
    """A boolean that is reported only after it held for `hold_s`."""

    def __init__(self):
        self.reported = None
        self.candidate = None
        self.since = None

    def update(self, value, now, hold_s):
        """Returns the newly confirmed value, or None when nothing changed."""
        if value != self.candidate:
            self.candidate, self.since = value, now
        if value != self.reported and now - self.since >= hold_s:
            self.reported = value
            return value
        return None

    def pause(self):
        self.candidate = self.since = None


class _FlowRun:
    """One flow sensor: confirmed flowing or not, and the current run."""

    def __init__(self):
        self.flowing = False
        self.candidate = False
        self.since = None
        self._clear()

    def _clear(self):
        self.started_at = None
        self.start_volume = None
        self.start_counter = None
        self.end = None              # (time, volume, counter, cause) of a pending stop
        self.samples = []
        self.clean = True            # no invalid reading during the run
        self.last_sample = None


class SignalWatch:
    def __init__(self, config, settings):
        self.cfg = config
        self.s = settings
        self.roles = {}
        for role, switch in config.level_roles.items():
            self.roles.setdefault(switch, []).append(role)
        self._levels = {}
        self._validity = {}
        self._flows = {}

    def update(self, inputs) -> list:
        """(kind, data) events for this cycle, in the order they happened."""
        events = []
        if not inputs.acquisition_ok:
            for held in self._validity.values():
                held.pause()
            self._flow_events(inputs, events)
            return events
        self._level_events(inputs, events)
        self._validity_events(inputs, events)
        self._flow_events(inputs, events)
        return events

    # Level switches ----------------------------------------------------------
    def _level_events(self, inputs, events):
        for name, signal in inputs.signals.levels.items():
            if not signal.valid or not isinstance(signal.reached, bool):
                continue
            previous = self._levels.get(name)
            if previous == signal.reached:
                continue
            self._levels[name] = signal.reached
            overridden = name in inputs.overridden
            events.append((LEVEL_CHANGED, {
                "sensor": name, "roles": self.roles.get(name, []),
                "old": previous, "new": signal.reached, "initial": previous is None,
                "overridden": overridden,
                "effective": inputs.effective.level(name) if overridden else signal.reached,
            }))

    # Validity ----------------------------------------------------------------
    def _sensors(self, signals):
        for name, level in signals.levels.items():
            yield name, level.valid, level.error
        for name, flow in signals.flows.items():
            yield name, flow.valid, flow.error
        for name, reading in signals.temperatures.items():
            yield name, reading.valid, reading.error
        feedback = signals.valve.feedback
        yield Y4_FEEDBACK, feedback.valid, feedback.error

    def _validity_events(self, inputs, events):
        hold = self.s.signal_validity_hold_s
        for name, valid, error in self._sensors(inputs.signals):
            held = self._validity.setdefault(name, _Held())
            initial = held.reported is None
            confirmed = held.update(bool(valid), inputs.now, hold)
            if confirmed is None or (initial and confirmed):
                continue                # no change, or valid at startup
            data = {"sensor": name, "valid": confirmed, "initial": initial,
                    "held_s": hold}
            if not confirmed:
                data["error"] = error
            events.append((SENSOR_VALIDITY, data))

    # Flow activity -------------------------------------------------------------
    def _registering(self, flow):
        if flow.flow_lpm is not None:
            return flow.flow_lpm >= self.s.flow_activity_min_l_min
        return (flow.pulse_rate_hz or 0.0) >= self.s.flow_activity_min_pulse_hz

    def _cause(self, inputs, flow):
        if not inputs.acquisition_ok:
            return "Acquisition unavailable"
        if flow is None or not flow.valid:
            return f"Measurement invalid: {getattr(flow, 'error', None)}"
        if flow.flow_lpm is not None:
            return f"Flow below {self.s.flow_activity_min_l_min:g} L/min"
        return f"Pulse rate below {self.s.flow_activity_min_pulse_hz:g} Hz (no K-factor)"

    def _flow_events(self, inputs, events):
        now, hold = inputs.now, self.s.flow_activity_hold_s
        for name, flow in inputs.signals.flows.items():
            run = self._flows.setdefault(name, _FlowRun())
            usable = inputs.acquisition_ok and flow.valid
            active = usable and self._registering(flow)

            if active != run.candidate:
                run.candidate, run.since = active, now
                if active and not run.flowing:          # possible start
                    run._clear()
                    run.started_at = now
                    run.start_volume = flow.measured_volume_litres
                    run.start_counter = flow.raw_counter
                elif active:                            # pending stop cancelled
                    run.end = None
                elif run.flowing:                       # possible stop
                    run.end = (now, flow.measured_volume_litres if usable else None,
                               flow.raw_counter if usable else None,
                               self._cause(inputs, flow))
                else:                                   # start was only a blip
                    run._clear()

            if run.started_at is not None:
                if not usable:
                    run.clean = False
                elif active and flow.sampled_at != run.last_sample:
                    run.last_sample = flow.sampled_at
                    run.samples.append(flow.flow_lpm)

            if active == run.flowing or now - run.since < hold:
                continue
            run.flowing = active
            if active:
                events.append((FLOW_STARTED, {
                    "sensor": name, "l_min": _round(flow.flow_lpm),
                    "pulse_hz": _round(flow.pulse_rate_hz), "calibrated": flow.calibrated,
                    "confirmed_after_s": round(now - run.started_at, 3)}))
            else:
                events.append((FLOW_STOPPED, self._stopped(name, run, flow)))
                run._clear()

    def _stopped(self, name, run, flow):
        ended, volume, counter, cause = run.end or (run.since, None, None, "Unknown")
        litres = (volume - run.start_volume
                  if volume is not None and run.start_volume is not None else None)
        pulses = None
        if run.clean and counter is not None and run.start_counter is not None:
            pulses = (counter - run.start_counter) & 0xFFFFFFFF
        values = [v for v in run.samples if v is not None]
        return {"sensor": name, "duration_s": round(ended - run.started_at, 3),
                "litres": _round(litres), "pulses": pulses,
                "avg_l_min": _round(sum(values) / len(values)) if values else None,
                "peak_l_min": _round(max(values)) if values else None,
                "calibrated": flow.calibrated, "cause": cause,
                "complete": run.clean}


def _round(value, digits=3):
    return None if value is None else round(value, digits)
