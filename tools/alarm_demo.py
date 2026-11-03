"""Demonstrate alarm behavior with pretend conditions."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.alarm_manager import AlarmManager
from pasteurizer.alarms import AlarmDefinition, AlarmScope, AlarmSeverity


NO_FLOW = AlarmDefinition(
    code="TRANSFER_1_NO_FLOW",
    scope=AlarmScope.TRANSFER_1,
    severity=AlarmSeverity.WARNING,
    message="Transfer pump 1 commanded on without measured flow",
    latching=True,
    recovery="Check pump 1 and flow sensor 1, then reset.",
)

manager = AlarmManager([NO_FLOW])
now = 0.0


def cycle(present):
    global now
    now += 0.2
    manager.start_cycle(now)
    manager.update(NO_FLOW.code, present)


# 1. A pretend no-flow condition appears.
cycle(True)
assert manager.has_warning()
assert manager.has_warning(AlarmScope.TRANSFER_1)
assert not manager.has_warning(AlarmScope.PASTEURIZER)
assert not manager.has_trip()
assert manager.current()[0].active
print("1. No flow detected: yellow warning, scoped to TRANSFER_1 only.")

# 2. Reset must not remove a condition that is still present.
assert manager.reset_cleared() == ()
assert len(manager.current()) == 1
print("2. Reset while fault is active: alarm remains.")

# 3. Acknowledging is not resetting.
manager.acknowledge(NO_FLOW.code)
assert manager.current()[0].acknowledged
assert manager.current()[0].active
print("3. Acknowledged: still active.")

# 4. Flow returns, but the alarm is latched.
cycle(False)
assert manager.has_warning()
assert not manager.current()[0].active
print("4. Flow restored: cleared alarm remains latched.")

# 5. Missing data in a later cycle: unknown never clears, and blocks reset.
cycle(None)
assert manager.reset_cleared() == ()
assert len(manager.current()) == 1
print("5. Condition unknown this cycle: reset refused.")

# 6. Cause freshly confirmed absent: reset removes the alarm.
cycle(False)
assert manager.reset_cleared() == (NO_FLOW.code,)
assert manager.current() == ()
assert not manager.has_warning()
print("6. Cause confirmed absent: reset accepted.")

# 7. Redefining a registered code is refused.
try:
    manager.register(AlarmDefinition(
        NO_FLOW.code, AlarmScope.TRANSFER_1, AlarmSeverity.TRIP, "changed"
    ))
except ValueError:
    print("7. Redefinition of an existing alarm code refused.")
else:
    raise AssertionError("Redefinition should fail")

kinds = [event.kind.value for event in manager.history()]
assert kinds == ["RAISED", "ACKNOWLEDGED", "CLEARED", "RESET"], kinds
print(f"History: {kinds}")

print("All alarm checks passed.")
