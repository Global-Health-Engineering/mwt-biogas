"""Layer 2: combine raw hardware snapshots into one processed SignalSnapshot.

Freshness is decided here, once, for every consumer:

- A snapshot older than acquisition.max_data_age_seconds, a read failure or a
  non-increasing timestamp gives an *unavailable* snapshot (acquisition.valid
  is False) and restarts all histories.
- A gap between samples longer than that limit marks the snapshot
  *interrupted*: histories restart, and equipment must not rely on it.
- Shorter gaps (up to missed_cycles_before_unavailable - 1 missed reads) are
  tolerated without any reset.

Equipment controllers use SignalSnapshot.available instead of checking time
themselves.
"""

from dataclasses import asdict
import math

from pasteurizer.clock import Clock, MonotonicClock
from pasteurizer.config import PlantConfig, load_config
from pasteurizer.contracts import (
    AcquisitionStatus,
    FlowSignal,
    LevelSignal,
    ModeSignal,
    SignalSnapshot,
    ValveSignals,
)
from pasteurizer.flows import FlowProcessor
from pasteurizer.levels import LevelProcessor
from pasteurizer.modes import Mode, process_mode
from pasteurizer.temperatures import TemperatureReading, process_temperature
from pasteurizer.valves import ValveFeedback, process_feedback


FLOW_EVALUATION_INTERVAL_S = 1.0


class SignalProcessor:
    def __init__(
        self,
        config: PlantConfig | None = None,
        *,
        config_directory=None,
        clock: Clock | None = None,
    ):
        self.config = config if config is not None else load_config(config_directory)
        self.clock = clock if clock is not None else MonotonicClock()
        self.max_age = self.config.acquisition.max_data_age_seconds

        self.levels = {
            name: LevelProcessor(
                active_high=options.active_high,
                debounce_seconds=options.debounce_seconds,
            )
            for name, options in self.config.levels.items()
            if options.enabled
        }

        self.flows = {
            name: FlowProcessor(**asdict(options))
            for name, options in self.config.flows.items()
        }

        self.reset()

    def reset(self):
        """Invalidate sampling history after a read failure or interruption."""
        self._last_sample = None
        self._reset_histories()

    def _reset_histories(self):
        self._last_flow_sample = None
        self._flow_states = {}

        for processor in self.levels.values():
            processor.reset()

        for processor in self.flows.values():
            processor.reset()

    # ------------------------------------------------------------------
    # Unavailable data
    # ------------------------------------------------------------------

    def unavailable(self, now=None, error="Acquisition unavailable", *,
                    sampled_at=None, sequence=None, age=None):
        """Snapshot for a cycle without usable data; every signal is invalid.

        Call this when HardwareReader.read() fails, so consumers always
        receive a complete snapshot.
        """
        if now is None:
            now = self.clock.monotonic()
        self.reset()

        return SignalSnapshot(
            sampled_at=sampled_at if sampled_at is not None else now,
            sequence=sequence,
            acquisition=AcquisitionStatus(
                valid=False,
                interrupted=True,
                age_seconds=age,
                interval_seconds=None,
                error=error,
            ),
            module_status=None,
            mode=ModeSignal(Mode.INVALID, False, error),
            levels={
                name: LevelSignal(None, None, False, False, error)
                for name in self.levels
            },
            flows={
                name: FlowSignal(None, None, None, None, False,
                                 processor.calibrated, error, None)
                for name, processor in self.flows.items()
            },
            temperatures={
                name: TemperatureReading(None, None, None, False, error)
                for name in self.config.io_mapping["temperatures"]
            },
            valve=ValveSignals(
                feedback=ValveFeedback(None, None, None, False, error),
                command_raw=None,
                output_status=None,
                output_healthy=False,
            ),
        )

    # ------------------------------------------------------------------
    # Normal processing
    # ------------------------------------------------------------------

    def process(self, snapshot, now=None) -> SignalSnapshot:
        """Process one raw snapshot from HardwareReader.read().

        now defaults to the processor's clock; pass it explicitly in
        simulations.
        """
        if now is None:
            now = self.clock.monotonic()

        sampled_at = snapshot.get("sampled_at")
        sequence = snapshot.get("sequence")

        if (
            not isinstance(sampled_at, (int, float))
            or isinstance(sampled_at, bool)
            or not math.isfinite(sampled_at)
        ):
            return self.unavailable(now, "Snapshot timestamp is invalid",
                                    sequence=sequence)

        sampled_at = float(sampled_at)
        age = now - sampled_at

        if not 0 <= age <= self.max_age:
            return self.unavailable(
                now, f"Snapshot is stale ({age:.2f} s old)",
                sampled_at=sampled_at, sequence=sequence, age=age,
            )

        interval = None
        interrupted = False

        if self._last_sample is not None:
            interval = sampled_at - self._last_sample

            if interval <= 0:
                return self.unavailable(
                    now, "Snapshot timestamps must increase",
                    sampled_at=sampled_at, sequence=sequence, age=age,
                )

            if interval > self.max_age:
                self._reset_histories()
                interrupted = True

        self._last_sample = sampled_at

        status = snapshot["module_status"]
        dio_input_fault = bool(status["dio"] & 0x00FF)
        di_input_fault = bool(status["di"] & 0x00FF)

        inputs = snapshot["digital_inputs"]

        return SignalSnapshot(
            sampled_at=sampled_at,
            sequence=sequence,
            acquisition=AcquisitionStatus(
                valid=True,
                interrupted=interrupted,
                age_seconds=age,
                interval_seconds=interval,
                error=(
                    f"Sampling gap of {interval:.2f} s; histories restarted"
                    if interrupted else None
                ),
            ),
            module_status=dict(status),
            mode=self._mode(inputs, dio_input_fault),
            levels=self._levels(inputs, sampled_at, di_input_fault),
            flows=self._flows(snapshot["flow_counters"], sampled_at,
                              dio_input_fault),
            temperatures=self._temperatures(snapshot["temperatures"]),
            valve=self._valve(snapshot["control_valve"]),
        )

    def _mode(self, inputs, dio_input_fault):
        if dio_input_fault:
            return ModeSignal(Mode.INVALID, False, "DIO input fault")

        mode = process_mode(inputs["manual_switch"], inputs["auto_switch"])
        if mode == Mode.INVALID:
            return ModeSignal(mode, False, "Selector contacts conflict")
        return ModeSignal(mode, True)

    def _levels(self, inputs, now, di_input_fault):
        levels = {}
        for name, processor in self.levels.items():
            if di_input_fault:
                processor.reset()
                levels[name] = LevelSignal(
                    raw=inputs[name], reached=None, pending=False,
                    valid=False, error="DI input fault",
                )
                continue

            reading = processor.update(inputs[name], now)
            levels[name] = LevelSignal(
                raw=reading.raw,
                reached=reading.reached,
                pending=reading.pending,
                valid=reading.reached is not None,
                error=(
                    "Waiting for stable input"
                    if reading.reached is None else None
                ),
            )
        return levels

    def _flows(self, counters, now, dio_input_fault):
        if dio_input_fault:
            for processor in self.flows.values():
                processor.reset()

            self._last_flow_sample = None
            self._flow_states = {
                name: FlowSignal(
                    raw_counter=counters[name],
                    pulse_rate_hz=None,
                    flow_lpm=None,
                    measured_volume_litres=None,
                    valid=False,
                    calibrated=processor.calibrated,
                    error="DIO input fault",
                    sampled_at=now,
                )
                for name, processor in self.flows.items()
            }

        elif (
            self._last_flow_sample is None
            or now - self._last_flow_sample >= FLOW_EVALUATION_INTERVAL_S
        ):
            self._flow_states = {
                name: FlowSignal(
                    **asdict(processor.update(counters[name], now)),
                    sampled_at=now,
                )
                for name, processor in self.flows.items()
            }
            self._last_flow_sample = now

        return dict(self._flow_states)

    def _temperatures(self, readings):
        result = {}
        for name, reading in readings.items():
            limits = self.config.temperatures.get(name)
            result[name] = process_temperature(
                reading["raw"],
                reading["status"],
                limits.plausible_min_c if limits else None,
                limits.plausible_max_c if limits else None,
            )
        return result

    def _valve(self, valve):
        settings = self.config.valve
        feedback = process_feedback(
            valve["feedback"],
            valve["feedback_status"],
            settings.feedback_closed_raw,
            settings.feedback_open_raw,
            settings.feedback_margin_percent,
        )
        return ValveSignals(
            feedback=feedback,
            command_raw=valve["command"],
            output_status=valve["command_status"],
            output_healthy=valve["command_status"] == 0,
        )
