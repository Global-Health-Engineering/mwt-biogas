"""Input stage: one immutable view of everything the cycle decides on.

Reads the snapshot, processes signals, tracks the selector mode, applies
manual expiry and level overrides, and judges the latest model result.
Every later stage works only on the CycleInputs it returns.
"""

from dataclasses import dataclass

from pasteurizer.contracts import SignalSnapshot
from pasteurizer.model_interface import ModelResult
from pasteurizer.modes import Mode
from pasteurizer.numbers import is_finite_number
from pasteurizer.transfer_flow import measured_flow
from pasteurizer.valves import ValveFeedback

TEMPERATURES = ("PT1_B2", "PT2_B3", "PT3_B4", "PT4_B5", "PT5_B6", "PT6_B7")


@dataclass(frozen=True)
class CycleInputs:
    now: float
    signals: SignalSnapshot          # actual readings
    effective: SignalSnapshot        # with level overrides applied
    overridden: frozenset            # level switches currently overridden
    acquisition_ok: bool
    acquisition_age_s: float
    selector_ok: bool
    mode: Mode | None                # None while selector or acquisition is invalid
    pt1: float | None
    pt2: float | None
    pt3: float | None
    pt4: float | None
    pt5: float | None
    pt6: float | None
    flow3: float | None              # fresh, valid treatment flow in L/min
    supply: bool | None              # effluent_tank_low reached
    y4_feedback: ValveFeedback
    y4_output_healthy: bool
    model: ModelResult | None
    model_ok: bool
    model_reason: str

    def level(self, switch: str) -> bool | None:
        return self.effective.level(switch) if self.acquisition_ok else None

    def y4_closed(self, tolerance_percent: float) -> bool:
        fb = self.y4_feedback
        return (fb.valid and fb.position_percent is not None
                and fb.position_percent <= tolerance_percent)

    @property
    def override_actual(self) -> dict:
        """Physical readings of overridden switches (shown beside overrides)."""
        return {name: self.signals.levels[name].reached
                for name in self.overridden if name in self.signals.levels}


class InputStage:
    def __init__(self, config, *, reader, signal_processor, manual, model, log, start_time):
        self.cfg = config
        self.reader = reader
        self.signal_processor = signal_processor
        self.manual = manual
        self.model = model
        self.log = log
        self.mode: Mode | None = None
        self.last_good_snapshot = start_time

    def read(self, now: float) -> CycleInputs:
        signals = self._acquire(now)
        acquisition_ok = signals.available
        selector_ok = acquisition_ok and signals.mode.valid
        if selector_ok:
            self._track_mode(signals.mode.value)
        self._expire_manual(now)

        if acquisition_ok:
            effective, overridden = self.manual.apply_overrides(signals)
        else:
            effective, overridden = signals, frozenset()

        temps = [self._temperature(effective, name) for name in TEMPERATURES]
        model_ok, model_reason = self._model_usability(now, temps[0], temps[1])
        supply_switch = self.cfg.level_roles["effluent_tank_low"]

        return CycleInputs(
            now=now,
            signals=signals,
            effective=effective,
            overridden=overridden,
            acquisition_ok=acquisition_ok,
            acquisition_age_s=now - self.last_good_snapshot,
            selector_ok=selector_ok,
            mode=self.mode if selector_ok else None,
            pt1=temps[0], pt2=temps[1], pt3=temps[2],
            pt4=temps[3], pt5=temps[4], pt6=temps[5],
            flow3=measured_flow(effective.flows.get("flow_sensor_3"), now),
            supply=effective.level(supply_switch) if acquisition_ok else None,
            y4_feedback=effective.valve.feedback,
            y4_output_healthy=effective.valve.output_healthy or not acquisition_ok,
            model=self.model.latest,
            model_ok=model_ok,
            model_reason=model_reason,
        )

    # ------------------------------------------------------------------
    def _acquire(self, now):
        try:
            snapshot = self.reader.read()
            # Freshness is judged after the read, on the processor's clock: a
            # real reader stamps its snapshot after the cycle started, so the
            # cycle's own `now` would give a negative age and reject every
            # live snapshot. (Simulated clocks do not move within a cycle.)
            signals = self.signal_processor.process(snapshot)
        except Exception as error:
            signals = self.signal_processor.unavailable(now, f"Read failed: {error}")
        if signals.acquisition.valid:
            self.last_good_snapshot = now
        return signals

    def _track_mode(self, mode):
        if mode == self.mode:
            return
        previous, self.mode = self.mode, mode
        cleared_requests, cleared_overrides = self.manual.clear()
        self.log.record("MODE_CHANGE", old=getattr(previous, "value", None),
                        new=mode.value, cleared_requests=cleared_requests,
                        cleared_overrides=cleared_overrides)

    def _expire_manual(self, now):
        requests, overrides = self.manual.expire(now)
        if requests or overrides:
            self.log.record("MANUAL_EXPIRED", requests=requests, overrides=overrides)

    @staticmethod
    def _temperature(snapshot, name):
        reading = snapshot.temperatures.get(name)
        if reading is None or not reading.valid or not is_finite_number(reading.celsius):
            return None
        return reading.celsius

    def _model_usability(self, now, pt1, pt2):
        ok, reason = self.model.usable(now, self.cfg.control.model_control_enabled)
        if ok and (pt1 is None or pt2 is None):
            return False, "PT1 or PT2 invalid"
        return ok, reason
