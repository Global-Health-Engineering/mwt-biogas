"""Builds the requested output frame and its permissions (spec sections 5, 10, 14).

Independent equipment (transfer pumps, manhole drain, overflow valves) has
its own level logic in every mode. Process equipment (heaters, circulation,
Y3/Y4) follows the plant state in AUTO and expiring commissioning requests
in MANUAL. Permissions only allow requests; the arbiter decides the frame.
"""

from dataclasses import dataclass

from pasteurizer.alarms import AlarmScope
from pasteurizer.heating import AssistHeater
from pasteurizer.manhole_drain import ManholeDrainController
from pasteurizer.modes import Mode
from pasteurizer.output_commands import (
    PERMISSIONED_COMMANDS,
    OutputCommands,
    OutputPermissions,
)
from pasteurizer.overflow_valves import OverflowValve
from pasteurizer.process_state import (
    SECOND_STAGE_STATES,
    TREATMENT_STATES,
    ProcessState as P,
    TreatmentPath,
)
from pasteurizer.transfer_pumps import TransferPumpController

LINES = (1, 2)
HEATING_COMMANDS = ("heater_3kw", "heater_1kw", "heating_circulation")
VALVE_COMMANDS = ("pasteurizer_isolation_open", "control_valve_percent")


@dataclass(frozen=True)
class RequestSet:
    commands: OutputCommands
    permissions: OutputPermissions
    release_permitted: bool     # treatment permission held and Y4 commanded open
    events: tuple = ()          # (kind, data) for the event log, e.g. a used-up drain STOP


class _Frame:
    """Mutable working copy while one request set is assembled."""

    def __init__(self):
        self.requests = {name: False for name in PERMISSIONED_COMMANDS}
        self.requests["control_valve_percent"] = 0.0
        self.permitted = set()
        self.reasons = {}
        self.events = []

    def request(self, name, value, permitted=True, reason=None):
        self.requests[name] = value
        if permitted:
            self.permitted.add(name)
        elif reason:
            self.reasons[name] = reason

    def result(self, release) -> RequestSet:
        return RequestSet(
            OutputCommands(**self.requests),
            OutputPermissions.only(*sorted(self.permitted), reasons=self.reasons),
            release,
            tuple(self.events),
        )


class OutputRequestBuilder:
    def __init__(self, config):
        self.cfg = config
        self.cs = config.control
        self.transfer = TransferPumpController(config)
        self.drain = ManholeDrainController(config)
        self.overflow = {line: OverflowValve(self.cs.overflow) for line in LINES}
        self.assist = AssistHeater(self.cs.heater_1kw)
        self.transfer_requested = {line: False for line in LINES}

    def build(self, inputs, flags, process, manual, faults, arbiter) -> RequestSet:
        frame = _Frame()
        manual_requests = manual.active_requests() if inputs.mode == Mode.MANUAL else {}
        self._transfer_pumps(frame, inputs, manual_requests, faults, arbiter)
        self._manhole_drain(frame, inputs, manual_requests, manual)
        self._overflow_valves(frame, inputs, manual_requests)
        release = False
        if inputs.mode == Mode.AUTO:
            release = self._auto_process(frame, inputs, flags, process, arbiter)
        else:
            self.assist.reset()
            if inputs.mode == Mode.MANUAL:
                self._manual_process(frame, inputs, flags, manual, manual_requests)
        return frame.result(release)

    # Independent equipment -------------------------------------------------
    def _transfer_pumps(self, frame, inputs, manual_requests, faults, arbiter):
        if inputs.mode == Mode.MANUAL:
            for line in LINES:
                self.transfer.manual_requests[line] = bool(
                    manual_requests.get(f"transfer_pump_{line}", False))
        decisions = {}
        if inputs.acquisition_ok:
            decisions = self.transfer.process(
                inputs.effective,
                heater_3kw_active=arbiter.possibly_on("heater_3kw", inputs.now),
                overridden=inputs.overridden)
        for line in LINES:
            name = f"transfer_pump_{line}"
            decision = decisions.get(name)
            on = bool(decision and decision.on)
            self.transfer_requested[line] = on
            blocked = (faults.level_listed(2, AlarmScope[f"TRANSFER_{line}"])
                       or faults.active(f"TRANSFER_{line}_FLOW_INVALID"))
            permitted = (inputs.acquisition_ok and not blocked
                         and inputs.mode in (Mode.AUTO, Mode.MANUAL))
            frame.request(name, on, permitted,
                          f"Transfer {line} fault" if blocked else None)

    def _manhole_drain(self, frame, inputs, manual_requests, manual):
        if not inputs.acquisition_ok:
            return
        requested = manual_requests.get("manhole_drain")     # {} outside MANUAL
        decision = self.drain.process(
            inputs.effective, inputs.now,
            manual_request=None if requested is None else bool(requested),
            issued_at=manual.issued_at("manhole_drain"))
        if decision.stop_ended_by_water:
            # Spec 10: a manual STOP lasts until a new water event. The request
            # is withdrawn, so the page's renewal cannot restore it.
            holder = manual.requests["manhole_drain"].user
            manual.release("manhole_drain")
            frame.events.append(("MANUAL_RELEASE_AUTOMATIC", {
                "command": "manhole_drain", "user": holder,
                "cause": "New water event ends the manual STOP; automatic drainage resumes"}))
        frame.request("manhole_drain", decision.request.on)    # off while STOP-inhibited

    def _overflow_valves(self, frame, inputs, manual_requests):
        for line in LINES:
            name = f"overflow_valve_{line}_open"
            level = inputs.level(self.cfg.level_roles[f"line_{line}_tank_overflow"])
            automatic = self.overflow[line].update(level, inputs.now)
            frame.request(name, automatic or bool(manual_requests.get(name, False)))

    # Process equipment, AUTO --------------------------------------------------
    def _auto_process(self, frame, inputs, flags, process, arbiter) -> bool:
        state = process.state
        frame.request("heater_3kw", state == P.PREHEAT_3KW,
                      flags.heating_ok and state == P.PREHEAT_3KW)
        second_stage = state in SECOND_STAGE_STATES
        permitted = flags.heating_ok and second_stage
        frame.request("heating_circulation", second_stage, permitted)
        frame.request("heater_1kw", self._assist(inputs, flags, process, second_stage),
                      permitted)
        if state not in TREATMENT_STATES:
            return False

        treatment_ok = (inputs.supply is True and inputs.acquisition_ok
                        and not flags.treatment_fault and not flags.summary.trip)
        frame.request("pasteurizer_isolation_open", True, treatment_ok)
        frame.request("control_valve_percent", float(process.treatment.y4_command),
                      treatment_ok)
        written = arbiter.last_written
        return treatment_ok and written is not None and written.control_valve_percent > 0

    def _assist(self, inputs, flags, process, second_stage) -> bool:
        h = self.cs.heater_1kw
        if process.path == TreatmentPath.FALLBACK:
            on_below, off_at = h.fallback_on_below_c, h.fallback_off_at_c
        else:
            on_below, off_at = h.normal_on_below_c, h.normal_off_at_c
        return self.assist.update(inputs.pt5, on_below, off_at,
                                  flags.heating_ok and second_stage, inputs.now)

    # Process equipment, MANUAL ------------------------------------------------
    def _manual_process(self, frame, inputs, flags, manual, manual_requests):
        trip_limit = self.cs.faults.overtemperature_trip_c
        pasteurizer_trip = flags.summary.pasteurizer_trip
        heating_ok = (inputs.acquisition_ok
                      and inputs.pt5 is not None and inputs.pt6 is not None
                      and inputs.pt5 < trip_limit and inputs.pt6 < trip_limit
                      and not pasteurizer_trip)
        for name in HEATING_COMMANDS:
            frame.request(name, bool(manual_requests.get(name, False)), heating_ok,
                          "Heating inhibited" if not heating_ok else None)

        valves_ok = inputs.acquisition_ok and (not pasteurizer_trip
                                               or manual.valve_test_active)
        reason = "Pasteurizer Level 3; valve test not allowed" if not valves_ok else None
        frame.request("pasteurizer_isolation_open",
                      bool(manual_requests.get("pasteurizer_isolation_open", False)),
                      valves_ok, reason)
        frame.request("control_valve_percent",
                      float(manual_requests.get("control_valve_percent", 0.0)),
                      valves_ok, reason)
