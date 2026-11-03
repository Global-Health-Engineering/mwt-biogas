"""GUI users, manual requests and level overrides (spec section 14).

Roles (AGREED):
- OPERATOR: START/STOP, acknowledge and reset eligible faults.
- COMMISSIONING: additionally MANUAL equipment requests, level overrides and
  settings changes. MANUAL requests and live overrides need the physical
  selector in MANUAL; all expiry times and interlocks stay active.

Valve test during a pasteurizer Level 3 (AGREED): a commissioning user may
explicitly allow Y3/Y4 operation for testing. The fault stays latched,
heating stays inhibited, the allowance and the valve requests expire after
30 s unless renewed, and everything is displayed and logged.

Overrides never hide a real protective condition: for destination-high and
overflow switches a real "reached" always wins; for low switches a real "not
reached" always wins. An invalid reading stays invalid.
"""

from dataclasses import dataclass, replace
from enum import Enum

from pasteurizer.contracts import SignalSnapshot
from pasteurizer.modes import Mode
from pasteurizer.output_commands import (
    CONTROL_VALVE_COMMAND,
    PERMISSIONED_COMMANDS,
)


class Role(str, Enum):
    OPERATOR = "OPERATOR"
    COMMISSIONING = "COMMISSIONING"


@dataclass(frozen=True)
class User:
    name: str
    role: Role


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    reason: str


# Outputs a commissioning user may request in MANUAL (not compressor or fan).
MANUAL_COMMANDS = tuple(
    name for name in PERMISSIONED_COMMANDS
    if name not in ("biogas_compressor", "cabinet_fan_disabled")
)

# Level roles whose real "reached" protects equipment (cannot be hidden).
REACHED_PROTECTS = ("effluent_tank_high", "line_1_tank_overflow", "line_2_tank_overflow")
# Level roles whose real "not reached" protects equipment.
NOT_REACHED_PROTECTS = ("line_1_tank_low", "line_2_tank_low", "effluent_tank_low")


@dataclass(frozen=True)
class _Timed:
    value: object
    expires_at: float | None
    user: str


def require(user, *roles) -> CommandResult | None:
    if not isinstance(user, User):
        return CommandResult(False, "Unknown user")
    if user.role not in roles:
        return CommandResult(False, f"Role {user.role.value} not permitted")
    return None


class ManualControl:
    def __init__(self, settings, level_roles: dict[str, str], *, simulated_io: bool):
        self.request_expiry = settings.request_expiry_s
        self.override_expiry = settings.override_expiry_s
        self.simulated_io = simulated_io
        self._roles_by_switch = {switch: role for role, switch in level_roles.items()}
        self.requests: dict[str, _Timed] = {}
        self.overrides: dict[str, _Timed] = {}
        self.valve_test: _Timed | None = None

    # ------------------------------------------------------------------
    def request(self, user, command, value, mode, now) -> CommandResult:
        refused = require(user, Role.COMMISSIONING)
        if refused:
            return refused
        if mode != Mode.MANUAL:
            return CommandResult(False, "Selector not in MANUAL")
        if command not in MANUAL_COMMANDS:
            return CommandResult(False, f"{command} cannot be requested manually")
        if command == CONTROL_VALVE_COMMAND:
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not 0.0 <= value <= 100.0):
                return CommandResult(False, "Y4 position must be 0-100 %")
            value = float(value)
        elif not isinstance(value, bool):
            return CommandResult(False, "Value must be true or false")
        self.requests[command] = _Timed(value, now + self.request_expiry, user.name)
        return CommandResult(True, "Request accepted (expires unless renewed)")

    def override(self, user, switch, value, mode, now) -> CommandResult:
        if switch not in self._roles_by_switch and not switch.startswith("level_switch_"):
            return CommandResult(False, f"Unknown level switch {switch}")
        if not isinstance(value, bool):
            return CommandResult(False, "Value must be true or false")
        if self.simulated_io:
            self.overrides[switch] = _Timed(value, None, getattr(user, "name", "simulation"))
            return CommandResult(True, "Simulated override active until removed")
        refused = require(user, Role.COMMISSIONING)
        if refused:
            return refused
        if mode != Mode.MANUAL:
            return CommandResult(False, "Selector not in MANUAL")
        self.overrides[switch] = _Timed(value, now + self.override_expiry, user.name)
        return CommandResult(True, "Commissioning override active for 10 s")

    def allow_valve_test(self, user, mode, now) -> CommandResult:
        refused = require(user, Role.COMMISSIONING)
        if refused:
            return refused
        if mode != Mode.MANUAL:
            return CommandResult(False, "Selector not in MANUAL")
        self.valve_test = _Timed(True, now + self.request_expiry, user.name)
        return CommandResult(True, "Y3/Y4 test allowed despite pasteurizer Level 3 "
                                   "(expires unless renewed; heating stays inhibited)")

    @property
    def valve_test_active(self) -> bool:
        return self.valve_test is not None

    def cancel_override(self, switch) -> bool:
        return self.overrides.pop(switch, None) is not None

    def clear(self) -> tuple[list[str], list[str]]:
        """Drop all requests and live overrides (mode change); returns what was cleared."""
        requests = list(self.requests)
        self.requests.clear()
        if self.valve_test is not None:
            requests.append("valve_test")
            self.valve_test = None
        overrides = [] if self.simulated_io else list(self.overrides)
        if not self.simulated_io:
            self.overrides.clear()
        return requests, overrides

    def expire(self, now) -> tuple[list[str], list[str]]:
        expired_requests = [k for k, v in self.requests.items()
                            if v.expires_at is not None and now >= v.expires_at]
        for key in expired_requests:
            del self.requests[key]
        expired_overrides = [k for k, v in self.overrides.items()
                             if v.expires_at is not None and now >= v.expires_at]
        for key in expired_overrides:
            del self.overrides[key]
        if self.valve_test is not None and now >= self.valve_test.expires_at:
            self.valve_test = None
            expired_requests.append("valve_test")
        return expired_requests, expired_overrides

    def active_requests(self) -> dict[str, object]:
        return {name: timed.value for name, timed in self.requests.items()}

    # ------------------------------------------------------------------
    def apply_overrides(self, signals: SignalSnapshot) -> tuple[SignalSnapshot, frozenset]:
        """Effective snapshot with overrides applied, plus the overridden switches."""
        if not self.overrides:
            return signals, frozenset()
        levels = dict(signals.levels)
        applied = set()
        for switch, timed in self.overrides.items():
            actual = levels.get(switch)
            if actual is None or not actual.valid or not isinstance(actual.reached, bool):
                continue  # invalid stays invalid
            role = self._roles_by_switch.get(switch)
            if role in REACHED_PROTECTS:
                value = actual.reached or timed.value
            elif role in NOT_REACHED_PROTECTS:
                value = actual.reached and timed.value
            else:
                value = timed.value
            levels[switch] = replace(actual, reached=value, pending=False,
                                     error="Overridden" if value != actual.reached
                                     else actual.error)
            applied.add(switch)
        return replace(signals, levels=levels), frozenset(applied)
