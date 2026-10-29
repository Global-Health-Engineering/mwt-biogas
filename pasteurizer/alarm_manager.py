"""Track active and latched alarms without controlling hardware."""

from pasteurizer.alarms import Alarm, AlarmScope, AlarmSeverity


class AlarmManager:
    def __init__(self):
        self._alarms: dict[str, Alarm] = {}

    def update(
        self,
        code: str,
        *,
        condition_present: bool,
        scope: AlarmScope,
        severity: AlarmSeverity,
        message: str,
        latched: bool = True,
    ):
        """Update one alarm from the latest equipment assessment."""
        if type(condition_present) is not bool:
            raise ValueError("condition_present must be True or False")

        if condition_present:
            self._alarms[code] = Alarm(
                code=code,
                scope=scope,
                severity=severity,
                message=message,
                active=True,
                latched=latched,
            )

        elif code in self._alarms:
            alarm = self._alarms[code]
            alarm.active = False

            if not alarm.latched:
                del self._alarms[code]

    def current(self) -> list[Alarm]:
        """Return active alarms and cleared alarms awaiting reset."""
        return list(self._alarms.values())

    def reset_cleared(self):
        """Remove cleared alarms; conditions still present remain."""
        self._alarms = {
            code: alarm
            for code, alarm in self._alarms.items()
            if alarm.active
        }

    def has_warning(self) -> bool:
        return any(
            alarm.severity == AlarmSeverity.WARNING
            for alarm in self._alarms.values()
        )

    def has_trip(self) -> bool:
        return any(
            alarm.severity == AlarmSeverity.TRIP
            for alarm in self._alarms.values()
        )