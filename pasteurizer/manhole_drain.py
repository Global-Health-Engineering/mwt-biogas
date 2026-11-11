"""Manhole drainage decisions; no hardware writes.

Timing (config/drain_settings.json):
- The pump starts only after water has been detected continuously for
  on_delay_seconds.
- It keeps running for off_delay_seconds after the level switch drops.
- Unknown level data or a manual STOP turn the pump off immediately and
  restart both timers.
- Drainage stays active in AUTO, MANUAL, OFF and with an invalid selector
  (spec sections 3 and 10); a plant-wide trip stops it through the arbiter.

Manual STOP (spec sections 10 and 14): in MANUAL a commissioning user's STOP
request inhibits automatic drainage. The inhibition lasts only while that
STOP request is active and ends at once when
  * the request is released, expires or is cleared (logout, session expiry),
  * the selector leaves MANUAL (every mode change), or
  * a new water event (level switch False -> True) arrives; the STOP is then
    used up: renewals do not restore it, a new press does.
A manual START (or no request) means automatic drainage. There is no manual
run without water: the pump only runs by the level logic above.
"""

from dataclasses import dataclass
import math

from pasteurizer.config import PlantConfig, load_config
from pasteurizer.contracts import EquipmentRequest, SignalSnapshot
from pasteurizer.modes import Mode


COMMAND = "manhole_drain"
_UNUSED = object()                  # no STOP used up


@dataclass(frozen=True)
class DrainDecision:
    request: EquipmentRequest
    manual_inhibited: bool
    level_reached: bool | None
    # Seconds until the pending start or stop takes effect; None if nothing pending.
    delay_remaining_s: float | None
    stop_ended_by_water: bool = False   # this scan: a new water event used up the STOP


class ManholeDrainController:
    def __init__(self, config: PlantConfig | None = None):
        self.config = config if config is not None else load_config()
        self.level_input = self.config.level_roles["manhole_water_present"]
        self.on_delay = self.config.drain.on_delay_seconds
        self.off_delay = self.config.drain.off_delay_seconds
        self.reset()

    def reset(self):
        """Reset at startup; later read failures should use process()."""
        self.mode = None
        self.manual_inhibited = False
        self.running = False
        self._previous_reached = None
        self._reached_since = None
        self._released_since = None
        self._used_stop = _UNUSED       # issued_at of a STOP used up by a water event

    def _stop(self):
        self.running = False
        self._reached_since = None
        self._released_since = None

    def process(self, signals: SignalSnapshot, now: float,
                manual_request: bool | None = None,
                issued_at: float | None = None) -> DrainDecision:
        """manual_request: the active MANUAL request (False = STOP, True =
        START/automatic, None = none); issued_at: when it was pressed."""
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if manual_request is not None and not isinstance(manual_request, bool):
            raise ValueError("manual_request must be True, False or None")

        mode = signals.mode.value if signals.mode.valid else Mode.INVALID
        if mode != self.mode:
            self._used_stop = _UNUSED   # every mode change ends a manual STOP
        self.mode = mode
        stop = mode == Mode.MANUAL and manual_request is False
        if not stop:
            self._used_stop = _UNUSED

        reached = signals.level(self.level_input)
        available = signals.available and reached is not None
        blocker = None
        ended_by_water = False

        if not available:
            # Unknown input must not manufacture a False -> True edge.
            self._previous_reached = None
            self.manual_inhibited = stop and self._used_stop != issued_at
            self._stop()
            reason, blocker = ("Level reading or sampling unavailable",
                               "LEVEL_UNAVAILABLE")
        else:
            new_water_event = (
                self._previous_reached is False and reached is True
            )
            self._previous_reached = reached
            if stop and new_water_event and self._used_stop != issued_at:
                self._used_stop = issued_at     # the STOP is used up
                ended_by_water = True
            self.manual_inhibited = stop and self._used_stop != issued_at

            if self.manual_inhibited:
                self._stop()
                reason, blocker = ("Automatic drainage manually inhibited",
                                   "MANUAL_STOP")
            else:
                reason = self._apply_delays(reached, now)

        return DrainDecision(
            request=EquipmentRequest(
                on=self.running,
                reason=reason,
                inhibited_by=(blocker,) if blocker else (),
            ),
            manual_inhibited=self.manual_inhibited,
            level_reached=reached if available else None,
            delay_remaining_s=self._remaining(reached, now) if available else None,
            stop_ended_by_water=ended_by_water,
        )

    def _apply_delays(self, reached, now):
        if reached:
            self._released_since = None
            if self._reached_since is None:
                self._reached_since = now
            if not self.running and now - self._reached_since >= self.on_delay:
                self.running = True
            return (
                "Water detected; drainage running"
                if self.running else "Water detected; start delay running"
            )

        self._reached_since = None
        if self._released_since is None:
            self._released_since = now
        if self.running and now - self._released_since >= self.off_delay:
            self.running = False
        return (
            "Level cleared; run-on delay running"
            if self.running else "No water at level switch"
        )

    def _remaining(self, reached, now):
        if reached and not self.running and self._reached_since is not None:
            return max(0.0, self.on_delay - (now - self._reached_since))
        if not reached and self.running and self._released_since is not None:
            return max(0.0, self.off_delay - (now - self._released_since))
        return None
