"""Demonstrate alarm behavior with pretend conditions."""

from pasteurizer.alarm_manager import AlarmManager
from pasteurizer.alarms import AlarmScope, AlarmSeverity


manager = AlarmManager()


def update_no_flow(condition_present):
    manager.update(
        "TRANSFER_1_NO_FLOW",
        condition_present=condition_present,
        scope=AlarmScope.TRANSFER_1,
        severity=AlarmSeverity.WARNING,
        message="Transfer pump 1 commanded on without measured flow",
        latched=True,
    )


# 1. A pretend no-flow condition appears.
update_no_flow(True)
assert manager.has_warning()
assert not manager.has_trip()
assert manager.current()[0].active
print("1. No flow detected: yellow warning recorded.")

# 2. Reset must not remove a condition that is still present.
manager.reset_cleared()
assert len(manager.current()) == 1
print("2. Reset while fault is active: alarm remains.")

# 3. Flow returns, but the alarm is latched.
update_no_flow(False)
assert manager.has_warning()
assert not manager.current()[0].active
print("3. Flow restored: cleared alarm remains latched.")

# 4. Reset now removes the cleared alarm.
manager.reset_cleared()
assert manager.current() == []
assert not manager.has_warning()
print("4. Reset after fault clears: alarm removed.")

print("All alarm checks passed.")