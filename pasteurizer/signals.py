"""Combine raw hardware snapshots into processed signal states."""

from dataclasses import asdict
import json
import math
from pathlib import Path
import time

from pasteurizer.flows import FlowProcessor
from pasteurizer.levels import LevelProcessor
from pasteurizer.modes import process_mode
from pasteurizer.temperatures import process_temperature
from pasteurizer.valves import process_feedback


class SignalProcessor:
    def __init__(self, config_directory=None):
        if config_directory is None:
            config_directory = (
                Path(__file__).resolve().parents[1] / "config"
            )

        def load(filename):
            with open(Path(config_directory) / filename) as file:
                return json.load(file)

        level_settings = load("level_settings.json")
        flow_settings = load("flow_settings.json")
        self.valve_settings = load("valve_settings.json")

        self.levels = {
            name: LevelProcessor(
                active_high=options["active_high"],
                debounce_seconds=options["debounce_seconds"],
            )
            for name, options in level_settings.items()
            if options["enabled"]
        }

        self.flows = {
            name: FlowProcessor(**options)
            for name, options in flow_settings.items()
        }

        self.reset()

    def reset(self):
        """Invalidate sampling history after a read failure or interruption."""
        self._last_sample = None
        self._last_flow_sample = None
        self._flow_states = {}

        for processor in self.levels.values():
            processor.reset()

        for processor in self.flows.values():
            processor.reset()

    def process(self, snapshot):
        now = float(snapshot["sampled_at"])
        age = time.monotonic() - now

        if not math.isfinite(now) or not 0 <= age <= 0.5:
            self.reset()
            raise RuntimeError("Snapshot timestamp is invalid or stale")

        interrupted = False

        if self._last_sample is not None:
            interval = now - self._last_sample

            if interval <= 0:
                self.reset()
                raise RuntimeError("Snapshot timestamps must increase")

            if interval > 0.5:
                self.reset()
                interrupted = True

        self._last_sample = now

        status = snapshot["module_status"]
        dio_input_fault = bool(status["dio"] & 0x00FF)
        di_input_fault = bool(status["di"] & 0x00FF)

        inputs = snapshot["digital_inputs"]

        mode = None
        if not dio_input_fault:
            mode = process_mode(
                inputs["manual_switch"],
                inputs["auto_switch"],
            ).value

        levels = {}
        for name, processor in self.levels.items():
            if di_input_fault:
                processor.reset()
                levels[name] = {
                    "raw": inputs[name],
                    "reached": None,
                    "pending": False,
                    "valid": False,
                    "error": "DI input fault",
                }
            else:
                reading = processor.update(inputs[name], now)
                levels[name] = {
                    **asdict(reading),
                    "valid": reading.reached is not None,
                    "error": (
                        "Waiting for stable input"
                        if reading.reached is None else None
                    ),
                }

        if dio_input_fault:
            for processor in self.flows.values():
                processor.reset()

            self._last_flow_sample = None
            self._flow_states = {
                name: {
                    "raw_counter": snapshot["flow_counters"][name],
                    "pulse_rate_hz": None,
                    "flow_lpm": None,
                    "measured_volume_litres": None,
                    "valid": False,
                    "calibrated": processor.calibrated,
                    "error": "DIO input fault",
                    "sampled_at": now,
                }
                for name, processor in self.flows.items()
            }

        elif (
            self._last_flow_sample is None
            or now - self._last_flow_sample >= 1.0
        ):
            self._flow_states = {
                name: {
                    **asdict(processor.update(
                        snapshot["flow_counters"][name], now
                    )),
                    "sampled_at": now,
                }
                for name, processor in self.flows.items()
            }
            self._last_flow_sample = now

        temperatures = {
            name: asdict(process_temperature(
                reading["raw"], reading["status"]
            ))
            for name, reading in snapshot["temperatures"].items()
        }

        valve = snapshot["control_valve"]
        settings = self.valve_settings

        feedback = process_feedback(
            valve["feedback"],
            valve["feedback_status"],
            settings["feedback_closed_raw"],
            settings["feedback_open_raw"],
            settings["feedback_margin_percent"],
        )

        return {
            "sampled_at": now,
            "sampling_interrupted": interrupted,
            "module_status": dict(status),
            "mode": {
                "value": mode,
                "valid": mode is not None and mode != "INVALID",
            },
            "levels": levels,
            "flows": {
                name: dict(reading)
                for name, reading in self._flow_states.items()
            },
            "temperatures": temperatures,
            "control_valve": {
                "feedback": asdict(feedback),
                "command_raw": valve["command"],
                "output_status": valve["command_status"],
                "output_healthy": valve["command_status"] == 0,
            },
        }