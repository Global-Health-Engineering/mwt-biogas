"""Evaluates every fault condition once per cycle (control spec section 12).

Each group of conditions has its own method. The result, FaultFlags, is the
only fault information the state machine and output stage use.
"""

from dataclasses import dataclass

from pasteurizer.controller.conditions import fallback_prerequisites, heating_permitted
from pasteurizer.controller.y4_monitor import Y4Monitor
from pasteurizer.fault_manager import FaultManager, FaultSummary
from pasteurizer.model_interface import ModelStatus
from pasteurizer.process_state import (
    ACTIVE_STATES,
    TREATMENT_STATES,
    ProcessState as P,
    TreatmentPath,
)
from pasteurizer.transfer_flow import TransferFlowSupervisor

LINES = (1, 2)


@dataclass(frozen=True)
class SystemHealth:
    """Controller-level conditions gathered outside the plant inputs."""

    watchdog_restart: bool
    storage_unreadable: bool
    state_save_failed: bool
    logger_failed: bool
    last_write_ok: bool
    historian_recovered: bool = False    # corrupt database replaced, not acknowledged


@dataclass(frozen=True)
class FaultFlags:
    summary: FaultSummary
    heating_fault: bool       # PT5/PT6 invalid: heating off now
    treatment_fault: bool     # anything that forbids release now
    heating_ok: bool          # heating permission (spec section 6)
    fallback_ready: bool      # fallback prerequisites hold


class _SensorWatch:
    """Starts watching a sensor once it fails where it matters.

    The watch stays on after the plant leaves that state, so escalation to
    Level 3 continues, and ends when the sensor is valid again.
    """

    def __init__(self):
        self.on = False

    def update(self, known: bool, invalid: bool, relevant: bool) -> bool | None:
        if known and invalid and relevant:
            self.on = True
        if known and not invalid:
            self.on = False
        if not known:
            return None
        return invalid if self.on else False


class FaultEvaluator:
    def __init__(self, config, faults: FaultManager, y4: Y4Monitor):
        self.cfg = config
        self.cs = config.control
        self.faults = faults
        self.y4 = y4
        self.transfer = {line: TransferFlowSupervisor(self.cs.transfer_flow)
                         for line in LINES}
        self._pt3 = _SensorWatch()
        self._flow3 = _SensorWatch()
        self._heating = _SensorWatch()

    def evaluate(self, inputs, process, *, system: SystemHealth, manual_heating: bool,
                 transfer_requested: dict, last_written) -> FaultFlags:
        self._system(inputs, system)
        self._treatment_sensors(inputs, process)
        self._heating_sensors(inputs, process, manual_heating)
        self._temperatures(inputs)
        self._preheat(inputs, process)
        self._valve(inputs, process)
        self._treatment_events(inputs, process)
        self._model(inputs, process)
        self._overflow(inputs)
        self._transfer(inputs, transfer_requested, last_written)
        return self.flags(inputs)

    def flags(self, inputs) -> FaultFlags:
        """Recompute after the state machine may have raised a fault."""
        f = self.faults
        summary = f.summary()
        heating_fault = f.active("PT5_INVALID") or f.active("PT6_INVALID")
        treatment_fault = (heating_fault or f.active("PT3_INVALID")
                           or f.active("FLOW3_INVALID")
                           or f.active("Y4_FEEDBACK_INVALID"))
        return FaultFlags(
            summary=summary,
            heating_fault=heating_fault,
            treatment_fault=treatment_fault,
            heating_ok=heating_permitted(self.cs, inputs, heating_fault=heating_fault,
                                         trip=summary.trip),
            fallback_ready=fallback_prerequisites(self.cs, inputs),
        )

    # ------------------------------------------------------------------
    def _system(self, inputs, system):
        f = self.faults
        max_age = self.cfg.acquisition.max_data_age_seconds
        f.set("ACQUISITION_UNAVAILABLE", not inputs.acquisition_ok)
        f.set("ACQUISITION_LOST", inputs.acquisition_age_s >= max_age)
        f.escalating("SELECTOR_INVALID", "SELECTOR_INVALID_PERSISTENT",
                     (not inputs.signals.mode.valid) if inputs.acquisition_ok else None,
                     self.cs.faults.selector_invalid_s)
        f.set("WATCHDOG_RESTART", system.watchdog_restart)
        f.set("FAULT_STORAGE", system.storage_unreadable)
        f.set("STATE_SAVE_FAILED", system.state_save_failed)
        f.set("LOGGER_FAILURE", system.logger_failed)
        f.set("HISTORIAN_DB_RECOVERED", system.historian_recovered)
        f.set("OUTPUT_WRITE_FAILED", True if not system.last_write_ok else
              (False if inputs.y4_feedback.valid else None))

    def _treatment_sensors(self, inputs, process):
        known = inputs.acquisition_ok
        treating = process.state in TREATMENT_STATES
        fs = self.cs.faults
        self.faults.escalating(
            "PT3_INVALID", "PT3_INVALID_PERSISTENT",
            self._pt3.update(known, inputs.pt3 is None, treating), fs.pt3_invalid_s)
        self.faults.escalating(
            "FLOW3_INVALID", "FLOW3_INVALID_PERSISTENT",
            self._flow3.update(known, inputs.flow3 is None, treating), fs.flow3_invalid_s)

    def _heating_sensors(self, inputs, process, manual_heating):
        heating = process.state in ACTIVE_STATES or manual_heating
        invalid = inputs.pt5 is None or inputs.pt6 is None
        watching = self._heating.update(inputs.acquisition_ok, invalid, heating)
        for code, value in (("PT5", inputs.pt5), ("PT6", inputs.pt6)):
            present = None if watching is None else (watching and value is None)
            self.faults.escalating(f"{code}_INVALID", f"{code}_INVALID_PERSISTENT",
                                   present, self.cs.faults.pt5_pt6_invalid_s)

    def _temperatures(self, inputs):
        f = self.faults
        known = inputs.acquisition_ok
        f.set("PT4_INVALID", (inputs.pt4 is None) if known else None)
        f.set("MODEL_INPUT_INVALID",
              (inputs.pt1 is None or inputs.pt2 is None) if known else None)
        f.set("OVERTEMPERATURE", self._overtemperature(inputs))

    def _overtemperature(self, inputs):
        """Trip at the limit; resettable only with both valid and below reset."""
        fs = self.cs.faults
        if not inputs.acquisition_ok:
            return None
        values = (inputs.pt5, inputs.pt6)
        if any(v is not None and v >= fs.overtemperature_trip_c for v in values):
            return True
        if not self.faults.listed("OVERTEMPERATURE"):
            return False
        cool = all(v is not None and v < fs.overtemperature_reset_below_c for v in values)
        return False if cool else None

    def _preheat(self, inputs, process):
        overdue = (process.state == P.PREHEAT_3KW and process.preheat_since is not None
                   and inputs.now - process.preheat_since > self.cs.preheat.max_duration_s)
        self.faults.set("PREHEAT_TIMEOUT", True if overdue else
                        (False if inputs.pt5 is not None else None))

    def _valve(self, inputs, process):
        relevant = process.state in ACTIVE_STATES or process.state == P.STOPPING
        check = self.y4.check(inputs, relevant=relevant)
        self.faults.escalating("Y4_FEEDBACK_INVALID", "Y4_FEEDBACK_LOST",
                               check.feedback_missing,
                               self.cfg.valve.movement_timeout_seconds)
        fault = check.position_timeout or check.output_fault
        self.faults.set("Y4_POSITION_TIMEOUT",
                        True if fault else (False if check.healthy else None))

    def _treatment_events(self, inputs, process):
        f = self.faults
        treatment = process.treatment
        stop = self.cs.treatment_stop
        f.set("LOW_TREATMENT_FLOW", treatment.retry_pending)
        # Raised by the state machine when the retry fails (latched).
        f.set("LOW_TREATMENT_FLOW_RETRY_FAILED", False)
        f.set("FALLBACK_FLOW_HIGH", treatment.high_flow_pending)
        f.set("REPEATED_INTERRUPTIONS",
              treatment.interruption_count(inputs.now) >= stop.interruption_count)

    def _model(self, inputs, process):
        unreliable = (self.cs.model_control_enabled and inputs.model is not None
                      and inputs.model.status == ModelStatus.UNRELIABLE)
        on_model_path = (process.path == TreatmentPath.MODEL
                         and process.state in (P.READY, P.MODEL_TREATMENT))
        self.faults.set("MODEL_UNRELIABLE", unreliable)
        self.faults.set("MODEL_UNRELIABLE_NO_FALLBACK",
                        unreliable and on_model_path
                        and not fallback_prerequisites(self.cs, inputs))

    def _overflow(self, inputs):
        for line in LINES:
            switch = self.cfg.level_roles[f"line_{line}_tank_overflow"]
            self.faults.set(f"OVERFLOW_{line}", inputs.level(switch))

    def _transfer(self, inputs, requested, last_written):
        for line in LINES:
            running = bool(last_written and getattr(last_written, f"transfer_pump_{line}"))
            status = self.transfer[line].update(
                requested.get(line, False), running,
                inputs.effective.flows.get(f"flow_sensor_{line}"), inputs.now)
            self.faults.set(f"TRANSFER_{line}_NO_FLOW", status.no_flow)
            self.faults.set(f"TRANSFER_{line}_LOW_FLOW", status.low_flow)
            self.faults.escalating(
                f"TRANSFER_{line}_FLOW_INVALID", f"TRANSFER_{line}_FLOW_INVALID_PERSISTENT",
                status.measurement_invalid if inputs.acquisition_ok else None,
                self.cs.faults.transfer_flow_invalid_s)
