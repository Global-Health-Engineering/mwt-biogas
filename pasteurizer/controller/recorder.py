"""Writes cycle events and periodic samples to the event log (spec section 13).

Events (on change only):
  OUTPUT_REQUESTED   requested commands changed, with the arbiter's notes
  OUTPUT_WRITTEN     a successfully written command changed
  OUTPUT_WRITE_FAILED / OUTPUT_WRITE_RECOVERED
                     (at shutdown the same kinds, with cause SHUTDOWN and the full frame)
  INDICATORS         light modes (blinking lights are not logged per toggle)
  MODEL_STATUS       model status, usability or reason changed
  ALARM_*            alarm raised, cleared, reset, acknowledged

Samples (every `periodic_interval_s`): sensor values with validity, model
result, plant state, requested and written commands, lights and alarms.
"""

from dataclasses import asdict

LIGHTS = ("light_green", "light_yellow", "light_red")


class CycleRecorder:
    def __init__(self, log, faults, logging_settings, *, start_time):
        self.log = log
        self.faults = faults
        self.interval = logging_settings.periodic_interval_s
        self._last_written = None
        self._last_requested = None
        self._last_indicators = None
        self._last_model = None
        self._write_failing = False
        self._failed_writes = 0
        self._last_event = 0
        self._next_sample = start_time

    # Outputs ---------------------------------------------------------------
    def requested(self, status):
        """Requested (pre-arbiter) commands, recorded when they change."""
        requested = {k: v for k, v in status.requested.items() if k not in LIGHTS}
        previous = self._last_requested or {}
        changes = {k: v for k, v in requested.items()
                   if self._last_requested is None or previous.get(k) != v}
        if changes:
            self.log.record("OUTPUT_REQUESTED", changes=changes,
                            arbitration_notes=status.arbitration_notes)
        self._last_requested = requested

    def write_result(self, frame, ok: bool, indicators, plant_safe: bool):
        if ok:
            self._written(frame, plant_safe)
            self._indicators(indicators)
            if self._write_failing:
                self.log.record("OUTPUT_WRITE_RECOVERED",
                                failed_writes=self._failed_writes)
            self._failed_writes = 0
        else:
            # Nothing counts as written. The status reports every failed
            # cycle; the log records the first failure and the count.
            self._failed_writes += 1
            if not self._write_failing:
                self.log.record("OUTPUT_WRITE_FAILED", requested=asdict(frame),
                                plant_safe_requested=plant_safe,
                                note="Requested frame not confirmed as written")
        self._write_failing = not ok

    def shutdown_frame(self, frame, ok: bool):
        """The safe frame commanded at shutdown and its write result, always logged."""
        full = asdict(frame)
        requested = {k: v for k, v in full.items() if k not in LIGHTS}
        previous = self._last_requested or {}
        self.log.record("OUTPUT_REQUESTED", cause="SHUTDOWN", frame=full,
                        changes={k: v for k, v in requested.items() if previous.get(k) != v})
        self._last_requested = requested
        if ok:
            changes = {k: v for k, v in full.items() if self._last_written is None
                       or getattr(self._last_written, k) != v}
            self.log.record("OUTPUT_WRITTEN", cause="SHUTDOWN", frame=full,
                            changes=changes, plant_safe=True)
            self._last_written = frame
        else:
            self.log.record("OUTPUT_WRITE_FAILED", cause="SHUTDOWN", requested=full,
                            plant_safe_requested=True,
                            note="Shutdown safe frame not confirmed as written")

    def _written(self, frame, plant_safe):
        changes = {k: v for k, v in asdict(frame).items()
                   if k not in LIGHTS and (
                       self._last_written is None
                       or getattr(self._last_written, k) != v)}
        if changes:
            self.log.record("OUTPUT_WRITTEN", changes=changes, plant_safe=plant_safe)
        self._last_written = frame

    def _indicators(self, indicators):
        if indicators == self._last_indicators:
            return
        self.log.record("INDICATORS", **{k: getattr(v, "value", v)
                                         for k, v in asdict(indicators).items()})
        self._last_indicators = indicators

    # Model and alarms ------------------------------------------------------
    def model(self, inputs):
        result = inputs.model
        state = (None if result is None else result.status.value,
                 inputs.model_ok, inputs.model_reason)
        if state == self._last_model:
            return
        self.log.record("MODEL_STATUS", status=state[0], usable=state[1], reason=state[2],
                        sequence=None if result is None else result.sequence,
                        parameter_version=None if result is None else result.parameter_version,
                        previous=None if self._last_model is None else {
                            "status": self._last_model[0], "usable": self._last_model[1],
                            "reason": self._last_model[2]})
        self._last_model = state

    def alarm_events(self):
        for event in self.faults.events_after(self._last_event):
            self.log.record("ALARM_" + event.kind.value, code=event.code,
                            scope=event.scope.value, message=event.message)
            self._last_event = event.number

    # Periodic samples ------------------------------------------------------
    def periodic(self, inputs, status):
        if inputs.now < self._next_sample:
            return
        self._next_sample = inputs.now + self.interval
        self.log.sample(**process_sample(inputs, status))


def process_sample(inputs, status) -> dict:
    signals = inputs.signals          # actual readings, never the overrides
    model = inputs.model
    return {
        "state": status.state,
        "path": status.path,
        "mode": status.mode,
        "stop_hold": status.stop_hold,
        "acquisition": {"valid": inputs.acquisition_ok,
                        "age_s": round(inputs.acquisition_age_s, 3)},
        "temperatures": {name: {"celsius": r.celsius, "valid": r.valid}
                         for name, r in signals.temperatures.items()},
        "flows": {name: {"l_min": f.flow_lpm, "valid": f.valid, "calibrated": f.calibrated}
                  for name, f in signals.flows.items()},
        "levels": {name: {"reached": lv.reached, "valid": lv.valid}
                   for name, lv in signals.levels.items()},
        "overridden_levels": sorted(inputs.overridden),
        "y4_feedback": {"percent": signals.valve.feedback.position_percent,
                        "valid": signals.valve.feedback.valid},
        "model": None if model is None else {
            "sequence": model.sequence,
            "status": model.status.value,
            "predicted_log_reduction": model.predicted_log_reduction,
            "achievable_flow_l_min": model.achievable_flow_l_min,
            "predicted_pt3_c": model.predicted_pt3_c,
            "residual_c": model.residual_c,
        },
        "model_usable": status.model_usable,
        "model_reason": status.model_reason,
        "requested": status.requested,
        "written": status.written,          # None: frame not confirmed written
        "write_ok": status.write_ok,
        "release_permitted": status.release_permitted,
        "lights": status.lights,
        "alarms": [a.code for a in status.alarms if a.active],
    }
