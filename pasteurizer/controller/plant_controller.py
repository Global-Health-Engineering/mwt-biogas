"""Plant controller: wires the parts together and runs one scan cycle.

Cycle (control spec section 2):
  1. inputs      read snapshot, signals, mode, overrides, model usability
  2. faults      evaluate every condition -> FaultFlags
  3. process     state machine (treatment delegated to TreatmentLogic)
  4. requests    requested frame and permissions
  5. energy      solar permission for the electric heaters (energy.py)
     arbiter     one complete frame (plant safe frame on a plant Level 3)
  6. write       outcome recorded; heartbeat only after a successful write
  7. persist, log, publish status

Logging only queues records; the historian stores them outside the scan.
Operator actions go through `self.commands` between cycles. `shutdown()`
records the orderly stop and flushes the historian.
"""

from dataclasses import dataclass

from pasteurizer.arbiter import IndicatorInputs, OutputArbiter
from pasteurizer.clock import Clock, MonotonicClock
from pasteurizer.controller.fault_evaluation import FaultEvaluator, SystemHealth
from pasteurizer.controller.inputs import InputStage
from pasteurizer.controller.operator_commands import OperatorCommands
from pasteurizer.controller.output_requests import HEATING_COMMANDS, OutputRequestBuilder
from pasteurizer.controller.recorder import CycleRecorder
from pasteurizer.controller.state_keeper import StateKeeper
from pasteurizer.controller.state_machine import ProcessStateMachine
from pasteurizer.controller.status import ControllerStatus, build_status
from pasteurizer.controller.treatment import TreatmentLogic
from pasteurizer.controller.y4_monitor import Y4Monitor
from pasteurizer.energy import (
    ARMED,
    HEATERS,
    MEASURING,
    NOT_INTERLOCKS,
    SETTLING,
    EnergyManager,
)

# Solar probe phases of a heater: switching it off now would fail the probe.
PROBE_PHASES = (ARMED, SETTLING, MEASURING)
from pasteurizer.event_log import EventLog
from pasteurizer.fault_manager import FaultManager
from pasteurizer.feed_pump import FeedPumpGuard
from pasteurizer.heater_load import Heater3kWLoadMonitor
from pasteurizer.model_interface import ModelResult, ModelSupervisor
from pasteurizer.modes import Mode
from pasteurizer.operator_interface import ManualControl
from pasteurizer.persistence import PersistentState
from pasteurizer.process_state import TreatmentPath
from pasteurizer.signals import SignalProcessor


def _data_source(reader) -> str:
    """Every reader must say whether it is the plant (LIVE) or a simulator."""
    source = getattr(reader, "data_source", None)
    if source not in ("LIVE", "SIMULATED"):
        raise ValueError("The input reader must declare data_source LIVE or SIMULATED")
    return source


def _outputs_enabled(writer) -> bool:
    """A writer without outputs (shadow run) says so; physical writers need not."""
    enabled = getattr(writer, "outputs_enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("The output writer's outputs_enabled must be True or False")
    return enabled


@dataclass(frozen=True)
class ShutdownResult:
    safe_frame_written: bool        # physically written; never True with outputs disabled
    state_saved: bool
    records_not_stored: int
    outputs_enabled: bool = True


class PlantController:
    def __init__(self, config, *, reader, writer, store, event_log: EventLog | None = None,
                 clock: Clock | None = None, simulated_io=False, heartbeat=None,
                 solar=None, publish=None):
        """solar: object with view() -> SolarLink view (pasteurizer/solar.py);
        None: no inverter data, so no solar permission for the electric heaters
        (unless energy.solar_permission_enabled is false)."""
        self.cfg = config
        self.publish = publish          # e.g. StatusPublisher.publish (reference swap)
        self.clock = clock if clock is not None else MonotonicClock()
        self.log = event_log if event_log is not None else EventLog(clock=self.clock)
        self.writer = writer
        # False in a shadow run: frames are accepted for supervision and the
        # record, but never reported as physically written.
        self.outputs_enabled = _outputs_enabled(writer)
        self.log.data_source = _data_source(reader)
        self.heartbeat = heartbeat
        self.solar = solar
        now = self.clock.monotonic()
        control = config.control

        self.faults = FaultManager()
        self.manual = ManualControl(control.manual, config.level_roles,
                                    simulated_io=simulated_io)
        self.model = ModelSupervisor(control.model.max_result_age_s)
        self.inputs = InputStage(config, reader=reader,
                                 signal_processor=SignalProcessor(config, clock=self.clock),
                                 manual=self.manual, model=self.model, log=self.log,
                                 start_time=now)
        self.y4 = Y4Monitor(config.valve)
        self.feed_pump = FeedPumpGuard(control)
        self.evaluator = FaultEvaluator(config, self.faults, self.y4, self.feed_pump)
        self.treatment = TreatmentLogic(config, self.log)
        self.process = ProcessStateMachine(config, self.treatment, self.faults, self.log,
                                           start_time=now)
        self.requests = OutputRequestBuilder(config, feed_pump=self.feed_pump)
        self.arbiter = OutputArbiter(config, start_time=now)
        self.energy = EnergyManager(config.arbiter.energy, start_time=now)
        self.heater_load = Heater3kWLoadMonitor(control)
        # Last cycle's requested frame and arbitration notes: the heat requests
        # and interlocks the solar probe sees (energy.update runs before them).
        self._last_requested = None
        self._last_notes = ()
        self._biogas_reported = None    # last biogas heat status in the event log
        self.recorder = CycleRecorder(self.log, self.faults, control.logging,
                                      start_time=now, outputs_enabled=self.outputs_enabled,
                                      config=config)
        self.keeper = StateKeeper(store, self.log)
        self.commands = OperatorCommands(
            process=self.process, treatment=self.treatment, faults=self.faults,
            manual=self.manual, inputs=self.inputs, keeper=self.keeper, log=self.log,
            clock=self.clock, persist=self._persist, energy=self.energy)

        self.cycle_count = 0
        self.last_write_ok = True
        self.status: ControllerStatus | None = None
        self._watchdog_restart = self._restore(now, simulated_io)

    # ------------------------------------------------------------------
    def _restore(self, now, simulated_io) -> bool:
        saved = self.keeper.restored
        self.process.stop_hold = saved.stop_hold
        self.treatment.retry_exhausted = saved.low_flow_retry_exhausted
        self.faults.start_cycle(now)
        self.faults.restore(saved.latched_faults, now)
        if saved.watchdog_tripped:
            # The watchdog marker is consumed: the fault is now latched instead.
            self.keeper.sync(self.persistent_state(), force=True)
        self._record_previous_run()
        self.log.restore_recovery_warning()
        self.log.record("CONTROLLER_START", config_hash=self.cfg.config_hash,
                        data_source=self.log.data_source,
                        clock_source=self.log.clock_source,
                        file_hashes=self.cfg.file_hashes,
                        restored_faults=saved.latched_faults, stop_hold=saved.stop_hold,
                        retry_exhausted=saved.low_flow_retry_exhausted,
                        watchdog_restart=saved.watchdog_tripped,
                        simulated_io=simulated_io,
                        outputs_enabled=self.outputs_enabled,
                        model_control_enabled=self.cfg.control.model_control_enabled)
        return saved.watchdog_tripped

    def _record_previous_run(self):
        """Compare with the last recorded run: unclean end, changed configuration."""
        previous = self.log.previous_boot()
        if previous is None:
            return
        if previous["stopped_utc"] is None:
            self.log.record("PREVIOUS_RUN_UNCLEAN", boot_id=previous["boot_id"],
                            last_record_utc=previous["last_utc"],
                            note="No orderly stop recorded (power loss, crash or watchdog)")
        if previous["config_hash"] not in (None, self.cfg.config_hash):
            old = previous["file_hashes"] or {}
            self.log.record("CONFIG_CHANGED", old_hash=previous["config_hash"],
                            new_hash=self.cfg.config_hash,
                            changed_files=sorted(name for name, digest
                                                 in self.cfg.file_hashes.items()
                                                 if old.get(name) != digest))

    def shutdown(self, reason: str, timeout_s: float = 5.0) -> "ShutdownResult":
        """Orderly stop, in this order so nothing can delay the safe outputs:

        1. write the plant safe command frame (immediately, no storage involved),
           queued as OUTPUT_REQUESTED / OUTPUT_WRITTEN (or _FAILED), cause SHUTDOWN;
           with outputs disabled (shadow run) OUTPUT_NOT_WRITTEN, never "written"
        2. save the controller state (latched faults, STOP hold)
        3. record CONTROLLER_STOP and flush the historian, bounded by timeout_s
        """
        now = self.clock.monotonic()
        frame = self.arbiter.safe_frame()
        accepted = self._write(frame, now, heartbeat=False)
        written = accepted and self.outputs_enabled
        self.recorder.shutdown_frame(frame, accepted)
        self._persist()
        self.log.record("CONTROLLER_STOP", reason=reason, cycles=self.cycle_count,
                        state=self.process.state.value, safe_frame_written=written,
                        outputs_enabled=self.outputs_enabled,
                        state_saved=self.keeper.saved)
        lost = self.log.close(timeout_s)
        return ShutdownResult(safe_frame_written=written, state_saved=self.keeper.saved,
                              records_not_stored=lost, outputs_enabled=self.outputs_enabled)

    def persistent_state(self) -> PersistentState:
        return PersistentState(
            latched_faults=[code for code in self.faults.latched_codes()
                            if code not in ("FAULT_STORAGE", "STATE_SAVE_FAILED")],
            stop_hold=self.process.stop_hold,
            low_flow_retry_exhausted=self.treatment.retry_exhausted,
            watchdog_tripped=False,
        )

    def _persist(self):
        self.keeper.sync(self.persistent_state())

    def submit_model_result(self, result: ModelResult) -> bool:
        accepted = self.model.submit(result)
        if not accepted:
            self.log.record("MODEL_RESULT_REJECTED", sequence=result.sequence)
        return accepted

    def reinitialize_fault_storage(self, user):
        return self.commands.reinitialize_fault_storage(user, self.persistent_state())

    # ------------------------------------------------------------------
    def cycle(self) -> ControllerStatus:
        self.cycle_count += 1
        self.log.cycle = self.cycle_count       # events of this scan carry its number
        try:
            return self._cycle()
        finally:
            self.log.cycle = None

    def _cycle(self) -> ControllerStatus:
        now = self.clock.monotonic()
        self.faults.start_cycle(now)

        inputs = self.inputs.read(now)
        self.recorder.signals(inputs)
        flags = self.evaluator.evaluate(
            inputs, self.process,
            system=self._system_health(),
            manual_heating=self._manual_heating(inputs),
            transfer_requested=self.requests.transfer_requested,
            last_written=self.arbiter.last_written)
        self._record_biogas_heat(inputs)
        # Solar permission first: the state machine waits for it before preheat.
        solar_view = self._solar_view()
        written = self.arbiter.last_written
        energy, energy_events = self.energy.update(
            solar_view, None if written is None else
            {name: getattr(written, name) for name in HEATERS}, now,
            requests=self._heat_requests(), interlocks=self._heater_interlocks())
        for kind, data in energy_events:
            self.log.record(kind, **data)
        thermostat = self.heater_load.update(energy_events, inputs, self.faults, self.log)
        self.process.step(inputs, flags, self.arbiter.last_written,
                          solar_3kw=(energy.permitted(["heater_3kw"]),
                                     self._solar_3kw_reason(energy)),
                          heater_3kw_thermostat=thermostat,
                          heater_3kw_blocked=self.heater_load.blocked(self.faults))
        flags = self.evaluator.flags(inputs)     # the step may latch a fault

        requests = self.requests.build(
            inputs, flags, self.process, self.manual, self.faults, self.arbiter,
            heater_3kw_probing=energy.modes["heater_3kw"].phase in PROBE_PHASES)
        for kind, data in requests.events:
            self.log.record(kind, **data)
        indicators = IndicatorInputs(
            process_state=self.process.state,
            warning=flags.summary.warning,
            trip=flags.summary.trip,
            release_permitted=requests.release_permitted,
            fallback_path=self.process.path == TreatmentPath.FALLBACK)
        result = self.arbiter.arbitrate(
            requests.commands, requests.permissions, indicators, now,
            energy=energy, plant_safe=flags.summary.plant_trip,
            feed_pump_sequence=inputs.mode == Mode.AUTO)

        self._last_requested = requests.commands
        self._last_notes = result.notes
        write_ok = self._write(result.commands, now)
        self.recorder.write_result(result.commands, write_ok, indicators,
                                   flags.summary.plant_trip)
        self._persist()
        self.recorder.alarm_events()

        self.status = build_status(
            cycle=self.cycle_count, inputs=inputs, process=self.process,
            requests=requests, result=result, write_ok=write_ok,
            keeper=self.keeper, faults=self.faults, manual=self.manual,
            historian=self.log.historian.status(), identity=self._identity(),
            lamp_modes=self.arbiter.indicator_modes(
                indicators, plant_safe=flags.summary.plant_trip),
            model_enabled=self.cfg.control.model_control_enabled,
            outputs_enabled=self.outputs_enabled,
            energy={"status": energy.to_dict(), "solar": solar_view,
                    "settings": self._energy_settings()},
            heating_wait=energy.waiting,
            heating=self._heating_status(now))
        self.recorder.requested(self.status, requests.explanations)
        self.recorder.model(inputs)
        self.recorder.periodic(inputs, self.status)
        if self.publish is not None:
            try:
                self.publish(self.status)
            except Exception:
                pass                    # the GUI path can never stop the scan
        return self.status

    # ------------------------------------------------------------------
    def _record_biogas_heat(self, inputs):
        """BIOGAS_HEAT event on every change of the diagnostic status."""
        status = self.evaluator.biogas_status
        current = None if status is None else status.status
        if current == self._biogas_reported:
            return
        self._biogas_reported = current
        self.log.record("BIOGAS_HEAT", status=current,
                        label=status.label if status else "Not evaluated",
                        delta_c=status.delta_c if status else None,
                        pt5=inputs.pt5, pt6=inputs.pt6,
                        circulation_s=status.circulation_s if status else None,
                        reason=status.reason if status else "")

    def _heating_status(self, now) -> dict:
        preheat = None
        if self.process.state.value in ("PREHEAT_3KW", "WAITING_FOR_SOLAR"):
            preheat = self.requests.preheat_circulation.status(self.process.circulation, now)
            preheat["pt5_refreshed"] = self.process.pt5_refreshed(now)
        biogas = self.evaluator.biogas_status
        return {"preheat_circulation": preheat,
                "biogas_heat": biogas.to_dict() if biogas is not None else None}

    def _heat_requests(self) -> dict:
        """Heat request per electric heater: the AUTO cold start (waiting for
        solar or preheating) for the 3 kW, otherwise last cycle's requested
        frame (AUTO assist, MANUAL ON)."""
        last = self._last_requested
        requests = {name: bool(last is not None and getattr(last, name)) for name in HEATERS}
        if self.process.state.value in ("WAITING_FOR_SOLAR", "PREHEAT_3KW"):
            requests["heater_3kw"] = True
        return requests

    def _heater_interlocks(self) -> dict:
        """Heaters another arbitration rule held off last cycle (not the energy
        rule, not the switching pause): such an interlock ends a probe."""
        return {n.output: f"{n.rule}: {n.message}" for n in self._last_notes
                if n.output in HEATERS and n.rule not in NOT_INTERLOCKS}

    @staticmethod
    def _solar_3kw_reason(energy) -> str:
        """Why the 3 kW has no solar permission, for the state transition. Before
        the cold start there is no heat request yet: name what else is missing."""
        mode = energy.modes["heater_3kw"]
        if mode.state == "RETRY_WAIT":           # live countdown: status heating_wait
            last = (mode.probe.get("last") or {}).get("reason", "solar probe ended")
            return f"{last}; next probe after the retry delay"
        if "heater_3kw" in energy.requests or mode.permitted:
            return mode.reason
        blockers = mode.probe.get("start_blockers") or []
        return "; ".join(blockers) if blockers else "solar probe starts with the heat request"

    def _solar_view(self):
        if self.solar is None:
            return None
        try:
            return self.solar.view()
        except Exception as error:      # never stops the scan; counts as no data
            return {"enabled": False, "reason": f"Solar view failed: {error}"}

    def _energy_settings(self) -> dict:
        e = self.cfg.arbiter.energy
        return {"solar_permission_enabled": e.solar_permission_enabled,
                "heater_power_w": dict(e.heater_power_w),
                "soc_start_min_percent": e.soc_start_min_percent,
                "soc_stop_at_percent": e.soc_stop_at_percent,
                "communication_timeout_s": e.communication_timeout_s,
                "probe_min_pv_w": e.probe_min_pv_w, "probe_settle_s": e.probe_settle_s,
                "probe_switch_on_timeout_s": e.probe_switch_on_timeout_s,
                "probe_measure_timeout_s": e.probe_measure_timeout_s,
                "probe_min_solar_contribution": e.probe_min_solar_contribution,
                "probe_max_battery_discharge_w": e.probe_max_battery_discharge_w,
                "probe_hold_s": e.probe_hold_s, "probe_retry_delay_s": e.probe_retry_delay_s,
                "battery_balance_tolerance_w": e.battery_balance_tolerance_w,
                "battery_balance_tolerance_fraction": e.battery_balance_tolerance_fraction,
                "probe_min_load_rise_fraction": e.probe_min_load_rise_fraction,
                "probe_load_loss_s": e.probe_load_loss_s,
                "probe_load_check_heaters": list(e.probe_load_check_heaters),
                "preheat_max_heating_s": self.cfg.control.preheat.max_duration_s,
                "preheat_heating_s": round(self.process.preheat_heated_s, 1)
                if self.process.state.value in ("PREHEAT_3KW", "WAITING_FOR_SOLAR")
                else None}

    def _system_health(self) -> SystemHealth:
        health = SystemHealth(
            watchdog_restart=self._watchdog_restart,
            storage_unreadable=self.keeper.unreadable,
            state_save_failed=self.keeper.save_failed,
            logger_failed=self.log.check_health(),
            historian_recovered=bool(self.log.recovery_pending),
            last_write_ok=self.last_write_ok)
        self._watchdog_restart = False     # raised once, then latched
        return health

    def _identity(self) -> dict:
        """Snapshot identity for the GUI header (memory only, no I/O)."""
        return {"data_source": self.log.data_source, "clock_source": self.log.clock_source,
                "clock_sync": self.log.sync_state, "boot_id": self.log.boot_id,
                "wall": self.log.wall_clock(), "config_hash": self.cfg.config_hash}

    def _manual_heating(self, inputs) -> bool:
        if inputs.mode != Mode.MANUAL:
            return False
        requests = self.manual.active_requests()
        return any(requests.get(name) for name in HEATING_COMMANDS)

    def _write(self, frame, now, heartbeat=True) -> bool:
        try:
            ok = bool(self.writer.write(frame))
        except Exception as error:
            ok = False
            self.log.record("WRITE_EXCEPTION", error=str(error))
        self.arbiter.record_write(frame, now, ok)
        if ok:
            self.y4.command_written(frame.control_valve_percent, now)
            if heartbeat and self.heartbeat is not None:
                self.heartbeat(now)
        self.last_write_ok = ok
        return ok
