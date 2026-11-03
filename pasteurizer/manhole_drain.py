"""Manhole drainage decisions; no hardware writes.

Timing (config/drain_settings.json):
- The pump starts only after water has been detected continuously for
  on_delay_seconds.
- It keeps running for off_delay_seconds after the level switch drops.
- Unknown level data or a manual STOP turn the pump off immediately and
  restart both timers.
- Drainage stays active in AUTO, MANUAL, OFF and with an invalid selector
  (spec sections 3 and 10); a plant-wide trip stops it through the arbiter.
"""

from dataclasses import dataclass
import math

from pasteurizer.config import PlantConfig, load_config
from pasteurizer.contracts import EquipmentRequest, SignalSnapshot
from pasteurizer.modes import Mode


COMMAND = "manhole_drain"


@dataclass(frozen=True)
class DrainDecision:
    request: EquipmentRequest
    manual_inhibited: bool
    level_reached: bool | None
    # Seconds until the pending start or stop takes effect; None if nothing pending.
    delay_remaining_s: float | None


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

    def _stop(self):
        self.running = False
        self._reached_since = None
        self._released_since = None

    def request_manual(self, enabled):
        """START clears inhibition; STOP inhibits automatic drainage."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if self.mode != Mode.MANUAL:
            raise RuntimeError("Manual commands require MANUAL mode")

        self.manual_inhibited = not enabled

    def process(self, signals: SignalSnapshot, now: float) -> DrainDecision:
        if not math.isfinite(now):
            raise ValueError("now must be finite")

        mode = signals.mode.value if signals.mode.valid else Mode.INVALID
        self.mode = mode

        # AUTO immediately cancels any manual stop override.
        if mode == Mode.AUTO:
            self.manual_inhibited = False

        reached = signals.level(self.level_input)
        available = signals.available and reached is not None
        blocker = None

        if not available:
            # Unknown input must not manufacture a False -> True edge.
            self._previous_reached = None
            self._stop()
            reason, blocker = ("Level reading or sampling unavailable",
                               "LEVEL_UNAVAILABLE")
        else:
            new_water_event = (
                self._previous_reached is False and reached is True
            )
            self._previous_reached = reached

            if mode == Mode.MANUAL and new_water_event:
                self.manual_inhibited = False

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
