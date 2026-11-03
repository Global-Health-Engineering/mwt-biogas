"""GUI commands (control spec sections 3, 12, 14).

Every command checks the user's role, is logged with user, outcome and
details, and never commands equipment ON by itself.
"""

from pasteurizer.modes import Mode
from pasteurizer.operator_interface import CommandResult, Role, require

OPERATORS = (Role.OPERATOR, Role.COMMISSIONING)


class OperatorCommands:
    def __init__(self, *, process, treatment, faults, manual, inputs, keeper,
                 log, clock, persist):
        self.process = process
        self.treatment = treatment
        self.faults = faults
        self.manual = manual
        self.inputs = inputs
        self.keeper = keeper
        self.log = log
        self.clock = clock
        self.persist = persist          # callable: save the persistent state now

    def _logged(self, action, user, result, **details) -> CommandResult:
        self.log.record("OPERATOR_ACTION", action=action,
                        user=getattr(user, "name", None),
                        role=getattr(getattr(user, "role", None), "value", None),
                        accepted=result.accepted, outcome=result.reason, **details)
        return result

    def _storage_refusal(self):
        if self.keeper.healthy:
            return None
        return CommandResult(False, "Persistent state not stored; resolve storage first")

    # START / STOP --------------------------------------------------------
    def start(self, user) -> CommandResult:
        refused = require(user, *OPERATORS) or self._storage_refusal()
        if refused:
            return self._logged("START", user, refused)
        if self.inputs.mode != Mode.AUTO:
            return self._logged("START", user, CommandResult(False, "Selector not in AUTO"))
        self.process.stop_hold = False
        self.persist()
        return self._logged("START", user, CommandResult(
            True, "STOP hold cleared; startup checks apply"))

    def stop(self, user) -> CommandResult:
        refused = require(user, *OPERATORS)
        if refused:
            return self._logged("STOP", user, refused)
        self.process.stop_hold = True
        self.persist()
        return self._logged("STOP", user, CommandResult(
            True, "Pasteurization stopping; held in IDLE until START"))

    # Faults ----------------------------------------------------------------
    def reset(self, user, scopes=None) -> CommandResult:
        refused = require(user, *OPERATORS) or self._storage_refusal()
        if refused:
            return self._logged("RESET", user, refused)
        codes = self.faults.reset(scopes)
        self.treatment.restore_retry()
        self.persist()
        remaining = [s.code for s in self.faults.current()
                     if s.definition.fault_level >= 2]
        return self._logged("RESET", user, CommandResult(
            True, f"Reset {list(codes)}; still latched {remaining}"),
            reset_codes=list(codes), still_latched=remaining)

    def acknowledge(self, user, code) -> CommandResult:
        refused = require(user, *OPERATORS)
        if refused:
            return self._logged("ACKNOWLEDGE", user, refused, code=code)
        self.faults.alarms.acknowledge(code)
        if code == "HISTORIAN_DB_RECOVERED":
            # The warning clears on acknowledgement; preserved files are kept.
            self.log.acknowledge_recovery(user)
        return self._logged("ACKNOWLEDGE", user, CommandResult(True, "Acknowledged"),
                            code=code)

    def reinitialize_fault_storage(self, user, state) -> CommandResult:
        refused = require(user, Role.COMMISSIONING)
        if refused:
            return self._logged("REINITIALIZE_STORAGE", user, refused)
        error = self.keeper.reinitialize(state)
        if error:
            return self._logged("REINITIALIZE_STORAGE", user,
                                CommandResult(False, f"Save failed: {error}"))
        return self._logged("REINITIALIZE_STORAGE", user, CommandResult(
            True, "Fault storage reinitialised; GUI reset still required"))

    # MANUAL ----------------------------------------------------------------
    def manual_request(self, user, command, value) -> CommandResult:
        result = self.manual.request(user, command, value, self.inputs.mode,
                                     self.clock.monotonic())
        return self._logged("MANUAL_REQUEST", user, result, command=command, value=value)

    def level_override(self, user, switch, value) -> CommandResult:
        result = self.manual.override(user, switch, value, self.inputs.mode,
                                      self.clock.monotonic())
        return self._logged("LEVEL_OVERRIDE", user, result, switch=switch, value=value)

    def cancel_override(self, user, switch) -> CommandResult:
        removed = self.manual.cancel_override(switch)
        return self._logged("OVERRIDE_CANCELLED", user, CommandResult(
            removed, "Removed" if removed else "None active"), switch=switch)

    def allow_valve_test(self, user) -> CommandResult:
        result = self.manual.allow_valve_test(user, self.inputs.mode,
                                              self.clock.monotonic())
        return self._logged("VALVE_TEST_OVERRIDE", user, result,
                            pasteurizer_trip_latched=self.faults.summary().pasteurizer_trip)
