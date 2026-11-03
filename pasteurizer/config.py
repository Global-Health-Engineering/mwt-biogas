"""Load and validate all plant configuration once.

Every controller receives the validated PlantConfig instead of opening JSON
files itself. Loading fails immediately, with the file and field named, if a
value has the wrong type, is out of range or refers to something that is not
configured elsewhere.
"""

from dataclasses import dataclass, fields
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from pasteurizer.control_settings import (
    ControlSettings,
    ControlSettingsError,
    parse_control_settings,
)
from pasteurizer.output_commands import OutputCommands, SWITCHED_COMMANDS


DEFAULT_CONFIG_DIRECTORY = Path(__file__).resolve().parents[1] / "config"

CONTROL_VALVE_EQUIPMENT = "control_valve_Y4"
MODE_CONTACTS = ("manual_switch", "auto_switch")


class ConfigError(ValueError):
    """Raised when a configuration file is missing, malformed or inconsistent."""


# ---------------------------------------------------------------------------
# Validated settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AcquisitionSettings:
    cycle_seconds: float
    missed_cycles_before_unavailable: int

    @property
    def max_data_age_seconds(self) -> float:
        """Data older than this, or a longer gap between samples, is unavailable."""
        return self.cycle_seconds * self.missed_cycles_before_unavailable


@dataclass(frozen=True)
class LevelSwitchSettings:
    enabled: bool
    active_high: bool
    debounce_seconds: float


@dataclass(frozen=True)
class FlowSensorSettings:
    pulses_per_litre: float | None
    calibration_confirmed: bool
    max_pulse_rate_hz: float | None
    max_sample_gap_seconds: float


@dataclass(frozen=True)
class TemperatureSensorSettings:
    plausible_min_c: float | None
    plausible_max_c: float | None


@dataclass(frozen=True)
class ValveSettings:
    command_closed_raw: int
    command_open_raw: int
    feedback_closed_raw: int
    feedback_open_raw: int
    feedback_margin_percent: float
    position_tolerance_percent: float
    movement_timeout_seconds: float | None


@dataclass(frozen=True)
class StartupSettings:
    water_temperature_sensor: str
    cold_start_below_celsius: float
    preheat_complete_at_celsius: float
    thresholds_validated: bool


@dataclass(frozen=True)
class DrainSettings:
    on_delay_seconds: float
    off_delay_seconds: float
    settings_validated: bool


@dataclass(frozen=True)
class ArbiterSettings:
    settings_validated: bool
    preheat_heater: str
    preheat_excluded_outputs: tuple[str, ...]
    switching_pause_seconds: float
    battery_level_available: bool
    # Separate battery permission per heater, e.g. {"heater_3kw": 60.0}.
    min_battery_percent: dict[str, float | None]
    isolation_open_before_control_valve_seconds: float
    compressor_enabled: bool
    cabinet_fan_disable_allowed: bool
    heat_up_blink_period_seconds: float


@dataclass(frozen=True)
class PlantConfig:
    directory: Path
    acquisition: AcquisitionSettings
    levels: dict[str, LevelSwitchSettings]
    flows: dict[str, FlowSensorSettings]
    temperatures: dict[str, TemperatureSensorSettings]
    temperature_ranges_validated: bool
    valve: ValveSettings
    startup: StartupSettings
    drain: DrainSettings
    arbiter: ArbiterSettings
    control: ControlSettings
    # role name -> level switch name, e.g. "line_1_tank_low" -> "level_switch_2"
    level_roles: dict[str, str]
    # OutputCommands field -> equipment identifier, e.g. "transfer_pump_1" -> "pump_120w_1_..."
    command_outputs: dict[str, str]
    # Raw documents for descriptive or model use. Treat as read-only.
    roles: dict[str, Any]
    io_mapping: dict[str, Any]
    treatment: dict[str, Any]
    # SHA-256 over every loaded file, for the historian and commissioning records.
    config_hash: str
    file_hashes: dict[str, str]     # per file, to name what changed between runs
    files: tuple[str, ...]


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _number(value, where, *, minimum=None, maximum=None,
            strictly_positive=False, allow_none=False, integer=False):
    if value is None and allow_none:
        return None
    if integer:
        if type(value) is not int:
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
    elif not _is_number(value):
        raise ConfigError(f"{where}: expected a finite number, got {value!r}")
    if strictly_positive and value <= 0:
        raise ConfigError(f"{where}: must be greater than zero")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{where}: must be at most {maximum}")
    return value if integer else float(value)


def _boolean(value, where):
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _text(value, where):
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _object(value, where):
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected an object")
    return value


def _exact_keys(data, expected, where):
    """Reject missing and unknown keys, which usually indicate typos."""
    data = _object(data, where)
    missing = set(expected) - set(data)
    unknown = set(data) - set(expected)
    if missing:
        raise ConfigError(f"{where}: missing {sorted(missing)}")
    if unknown:
        raise ConfigError(f"{where}: unknown {sorted(unknown)}")
    return data


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _acquisition(data, where):
    _exact_keys(data, ("cycle_seconds", "missed_cycles_before_unavailable"), where)
    return AcquisitionSettings(
        cycle_seconds=_number(
            data["cycle_seconds"], f"{where}.cycle_seconds",
            strictly_positive=True, maximum=5.0,
        ),
        missed_cycles_before_unavailable=_number(
            data["missed_cycles_before_unavailable"],
            f"{where}.missed_cycles_before_unavailable",
            integer=True, minimum=1, maximum=50,
        ),
    )


def _levels(data, where):
    result = {}
    for name, options in _object(data, where).items():
        here = f"{where}.{name}"
        _exact_keys(options, ("enabled", "active_high", "debounce_seconds"), here)
        result[name] = LevelSwitchSettings(
            enabled=_boolean(options["enabled"], f"{here}.enabled"),
            active_high=_boolean(options["active_high"], f"{here}.active_high"),
            debounce_seconds=_number(
                options["debounce_seconds"], f"{here}.debounce_seconds",
                minimum=0.0, maximum=60.0,
            ),
        )
    return result


def _flows(data, where):
    from pasteurizer.flows import FlowProcessor

    keys = ("pulses_per_litre", "calibration_confirmed",
            "max_pulse_rate_hz", "max_sample_gap_seconds")
    result = {}
    for name, options in _object(data, where).items():
        here = f"{where}.{name}"
        _exact_keys(options, keys, here)
        settings = FlowSensorSettings(
            pulses_per_litre=_number(
                options["pulses_per_litre"], f"{here}.pulses_per_litre",
                strictly_positive=True, allow_none=True,
            ),
            calibration_confirmed=_boolean(
                options["calibration_confirmed"], f"{here}.calibration_confirmed"
            ),
            max_pulse_rate_hz=_number(
                options["max_pulse_rate_hz"], f"{here}.max_pulse_rate_hz",
                strictly_positive=True, allow_none=True,
            ),
            max_sample_gap_seconds=_number(
                options["max_sample_gap_seconds"], f"{here}.max_sample_gap_seconds",
                strictly_positive=True,
            ),
        )
        # Reuse the processor's own checks (e.g. confirmed calibration needs a factor).
        try:
            FlowProcessor(**vars(settings))
        except ValueError as error:
            raise ConfigError(f"{here}: {error}") from None
        result[name] = settings
    return result


def _temperatures(data, where):
    _exact_keys(data, ("ranges_validated", "sensors"), where)
    result = {}
    for name, options in _object(data["sensors"], f"{where}.sensors").items():
        here = f"{where}.sensors.{name}"
        _exact_keys(options, ("plausible_min_c", "plausible_max_c"), here)
        low = _number(options["plausible_min_c"], f"{here}.plausible_min_c",
                      minimum=-200.0, maximum=850.0, allow_none=True)
        high = _number(options["plausible_max_c"], f"{here}.plausible_max_c",
                       minimum=-200.0, maximum=850.0, allow_none=True)
        if low is not None and high is not None and low >= high:
            raise ConfigError(f"{here}: plausible_min_c must be below plausible_max_c")
        result[name] = TemperatureSensorSettings(low, high)
    validated = _boolean(data["ranges_validated"], f"{where}.ranges_validated")
    return result, validated


def _valve(data, where):
    keys = ("command_closed_raw", "command_open_raw", "feedback_closed_raw",
            "feedback_open_raw", "feedback_margin_percent",
            "position_tolerance_percent", "movement_timeout_seconds")
    _exact_keys(data, keys, where)
    settings = ValveSettings(
        command_closed_raw=_number(data["command_closed_raw"],
                                   f"{where}.command_closed_raw", integer=True),
        command_open_raw=_number(data["command_open_raw"],
                                 f"{where}.command_open_raw", integer=True),
        feedback_closed_raw=_number(data["feedback_closed_raw"],
                                    f"{where}.feedback_closed_raw", integer=True),
        feedback_open_raw=_number(data["feedback_open_raw"],
                                  f"{where}.feedback_open_raw", integer=True),
        feedback_margin_percent=_number(data["feedback_margin_percent"],
                                        f"{where}.feedback_margin_percent",
                                        minimum=0.0, maximum=20.0),
        position_tolerance_percent=_number(data["position_tolerance_percent"],
                                           f"{where}.position_tolerance_percent",
                                           minimum=0.0, maximum=20.0),
        movement_timeout_seconds=_number(data["movement_timeout_seconds"],
                                         f"{where}.movement_timeout_seconds",
                                         strictly_positive=True, allow_none=True),
    )
    if settings.command_closed_raw == settings.command_open_raw:
        raise ConfigError(f"{where}: command endpoints must differ")
    if settings.feedback_closed_raw == settings.feedback_open_raw:
        raise ConfigError(f"{where}: feedback endpoints must differ")
    return settings


def _startup(data, where):
    keys = ("water_temperature_sensor", "cold_start_below_celsius",
            "preheat_complete_at_celsius", "thresholds_validated")
    _exact_keys(data, keys, where)
    settings = StartupSettings(
        water_temperature_sensor=_text(data["water_temperature_sensor"],
                                       f"{where}.water_temperature_sensor"),
        cold_start_below_celsius=_number(data["cold_start_below_celsius"],
                                         f"{where}.cold_start_below_celsius",
                                         minimum=0.0, maximum=100.0),
        preheat_complete_at_celsius=_number(data["preheat_complete_at_celsius"],
                                            f"{where}.preheat_complete_at_celsius",
                                            minimum=0.0, maximum=100.0),
        thresholds_validated=_boolean(data["thresholds_validated"],
                                      f"{where}.thresholds_validated"),
    )
    if settings.cold_start_below_celsius >= settings.preheat_complete_at_celsius:
        raise ConfigError(
            f"{where}: cold_start_below_celsius must be below "
            "preheat_complete_at_celsius"
        )
    return settings


def _drain(data, where):
    _exact_keys(data, ("on_delay_seconds", "off_delay_seconds",
                       "settings_validated"), where)
    return DrainSettings(
        on_delay_seconds=_number(data["on_delay_seconds"],
                                 f"{where}.on_delay_seconds",
                                 minimum=0.0, maximum=600.0),
        off_delay_seconds=_number(data["off_delay_seconds"],
                                  f"{where}.off_delay_seconds",
                                  minimum=0.0, maximum=600.0),
        settings_validated=_boolean(data["settings_validated"],
                                    f"{where}.settings_validated"),
    )


def _switched_list(value, where):
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list")
    for name in value:
        if name not in SWITCHED_COMMANDS:
            raise ConfigError(f"{where}: {name!r} is not a switched output command")
    if len(set(value)) != len(value):
        raise ConfigError(f"{where}: duplicate entries")
    return tuple(value)


def _arbiter(data, where):
    _exact_keys(data, ("settings_validated", "preheat_exclusion", "energy",
                       "isolation_valve", "compressor_enabled",
                       "cabinet_fan_disable_allowed", "indicators"), where)
    preheat = _exact_keys(data["preheat_exclusion"],
                          ("heater", "excluded_outputs", "switching_pause_seconds"),
                          f"{where}.preheat_exclusion")
    energy = _exact_keys(data["energy"],
                         ("battery_level_available", "min_battery_percent"),
                         f"{where}.energy")
    isolation = _exact_keys(data["isolation_valve"],
                            ("open_before_control_valve_seconds",),
                            f"{where}.isolation_valve")
    indicators = _exact_keys(data["indicators"],
                             ("heat_up_blink_period_seconds",),
                             f"{where}.indicators")

    heater = _text(preheat["heater"], f"{where}.preheat_exclusion.heater")
    if heater not in SWITCHED_COMMANDS:
        raise ConfigError(f"{where}.preheat_exclusion.heater: unknown output {heater!r}")
    excluded = _switched_list(preheat["excluded_outputs"],
                              f"{where}.preheat_exclusion.excluded_outputs")
    if heater in excluded:
        raise ConfigError(f"{where}.preheat_exclusion: heater cannot exclude itself")

    battery_available = _boolean(energy["battery_level_available"],
                                 f"{where}.energy.battery_level_available")
    minimums = _exact_keys(energy["min_battery_percent"],
                           ("heater_3kw", "heater_1kw"),
                           f"{where}.energy.min_battery_percent")
    min_battery = {
        name: _number(value, f"{where}.energy.min_battery_percent.{name}",
                      minimum=0.0, maximum=100.0, allow_none=True)
        for name, value in minimums.items()
    }
    if battery_available and None in min_battery.values():
        raise ConfigError(
            f"{where}.energy: every min_battery_percent is required when "
            "battery_level_available is true"
        )

    return ArbiterSettings(
        settings_validated=_boolean(data["settings_validated"],
                                    f"{where}.settings_validated"),
        preheat_heater=heater,
        preheat_excluded_outputs=excluded,
        switching_pause_seconds=_number(
            preheat["switching_pause_seconds"],
            f"{where}.preheat_exclusion.switching_pause_seconds",
            minimum=0.0, maximum=600.0),
        battery_level_available=battery_available,
        min_battery_percent=min_battery,
        isolation_open_before_control_valve_seconds=_number(
            isolation["open_before_control_valve_seconds"],
            f"{where}.isolation_valve.open_before_control_valve_seconds",
            minimum=0.0, maximum=600.0),
        compressor_enabled=_boolean(data["compressor_enabled"],
                                    f"{where}.compressor_enabled"),
        cabinet_fan_disable_allowed=_boolean(
            data["cabinet_fan_disable_allowed"],
            f"{where}.cabinet_fan_disable_allowed"),
        heat_up_blink_period_seconds=_number(
            indicators["heat_up_blink_period_seconds"],
            f"{where}.indicators.heat_up_blink_period_seconds",
            minimum=0.2, maximum=10.0),
    )


def _control(data):
    try:
        return parse_control_settings(data)
    except ControlSettingsError as error:
        raise ConfigError(str(error)) from None


# ---------------------------------------------------------------------------
# Cross-file checks
# ---------------------------------------------------------------------------

def _check_io_mapping(io_mapping):
    where = "io_mapping.json"
    for section in ("digital_inputs", "flow_counters", "temperatures",
                    "digital_outputs", CONTROL_VALVE_EQUIPMENT):
        _object(io_mapping.get(section), f"{where}.{section}")

    for contact in MODE_CONTACTS:
        if contact not in io_mapping["digital_inputs"]:
            raise ConfigError(f"{where}.digital_inputs: missing {contact!r}")

    for name, channels in io_mapping["temperatures"].items():
        _exact_keys(channels, ("value", "status"), f"{where}.temperatures.{name}")

    _exact_keys(io_mapping[CONTROL_VALVE_EQUIPMENT],
                ("command", "command_status", "feedback", "feedback_status"),
                f"{where}.{CONTROL_VALVE_EQUIPMENT}")

    channels = list(io_mapping["digital_outputs"].values())
    duplicates = {channel for channel in channels if channels.count(channel) > 1}
    if duplicates:
        raise ConfigError(
            f"{where}.digital_outputs: channels assigned twice {sorted(duplicates)}"
        )


def _check_cross_references(config_parts):
    io_mapping = config_parts["io_mapping"]
    levels = config_parts["levels"]
    flows = config_parts["flows"]
    temperatures = config_parts["temperatures"]
    startup = config_parts["startup"]
    level_roles = config_parts["level_roles"]
    command_outputs = config_parts["command_outputs"]

    inputs = io_mapping["digital_inputs"]
    for name in levels:
        if name not in inputs:
            raise ConfigError(
                f"level_settings.json: {name!r} has no digital input in io_mapping.json"
            )

    if set(flows) != set(io_mapping["flow_counters"]):
        raise ConfigError(
            "flow_settings.json and io_mapping.json flow_counters must list "
            "the same sensors"
        )

    mapped_temperatures = set(io_mapping["temperatures"])
    unknown = set(temperatures) - mapped_temperatures
    if unknown:
        raise ConfigError(
            f"temperature_settings.json: unmapped sensors {sorted(unknown)}"
        )

    if startup.water_temperature_sensor not in mapped_temperatures:
        raise ConfigError(
            "startup_settings.json: water_temperature_sensor "
            f"{startup.water_temperature_sensor!r} is not mapped"
        )

    for role, switch in level_roles.items():
        if switch not in levels:
            raise ConfigError(
                f"equipment_roles.json levels.{role}: {switch!r} is not configured "
                "in level_settings.json"
            )
        if not levels[switch].enabled:
            raise ConfigError(
                f"equipment_roles.json levels.{role}: {switch!r} is disabled "
                "in level_settings.json"
            )

    expected = {field.name for field in fields(OutputCommands)}
    if set(command_outputs) != expected:
        missing = sorted(expected - set(command_outputs))
        unknown = sorted(set(command_outputs) - expected)
        raise ConfigError(
            "equipment_roles.json command_outputs must list every OutputCommands "
            f"field exactly once (missing {missing}, unknown {unknown})"
        )

    digital_outputs = io_mapping["digital_outputs"]
    used = []
    for command, equipment in command_outputs.items():
        if command == "control_valve_percent":
            if equipment != CONTROL_VALVE_EQUIPMENT:
                raise ConfigError(
                    "equipment_roles.json command_outputs.control_valve_percent "
                    f"must be {CONTROL_VALVE_EQUIPMENT!r}"
                )
            continue
        if equipment not in digital_outputs:
            raise ConfigError(
                f"equipment_roles.json command_outputs.{command}: {equipment!r} "
                "is not a digital output in io_mapping.json"
            )
        used.append(equipment)

    if len(used) != len(set(used)):
        raise ConfigError(
            "equipment_roles.json command_outputs: an equipment output is "
            "assigned to more than one command"
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

FILES = (
    "acquisition_settings.json",
    "arbiter_settings.json",
    "control_settings.json",
    "drain_settings.json",
    "equipment_roles.json",
    "flow_settings.json",
    "io_mapping.json",
    "level_settings.json",
    "startup_settings.json",
    "temperature_settings.json",
    "treatment_settings.json",
    "valve_settings.json",
)


def load_config(directory=None) -> PlantConfig:
    directory = Path(directory) if directory is not None else DEFAULT_CONFIG_DIRECTORY

    documents = {}
    for filename in FILES:
        path = directory / filename
        try:
            with open(path, encoding="utf-8") as file:
                documents[filename] = json.load(file)
        except FileNotFoundError:
            raise ConfigError(f"Missing configuration file: {path}") from None
        except json.JSONDecodeError as error:
            raise ConfigError(f"{filename}: invalid JSON ({error})") from None

    digest = hashlib.sha256()
    file_hashes = {}
    for filename in FILES:
        canonical = json.dumps(documents[filename], sort_keys=True,
                               separators=(",", ":"))
        digest.update(filename.encode())
        digest.update(canonical.encode())
        file_hashes[filename] = hashlib.sha256(canonical.encode()).hexdigest()

    roles = documents["equipment_roles.json"]
    io_mapping = documents["io_mapping.json"]
    _check_io_mapping(io_mapping)

    level_roles = {
        role: _text(switch, f"equipment_roles.json levels.{role}")
        for role, switch in _object(roles.get("levels"),
                                    "equipment_roles.json levels").items()
    }
    command_outputs = {
        command: _text(equipment, f"equipment_roles.json command_outputs.{command}")
        for command, equipment in _object(
            roles.get("command_outputs"), "equipment_roles.json command_outputs"
        ).items()
    }

    temperatures, temperature_ranges_validated = _temperatures(
        documents["temperature_settings.json"], "temperature_settings.json"
    )

    parts = {
        "acquisition": _acquisition(documents["acquisition_settings.json"],
                                    "acquisition_settings.json"),
        "levels": _levels(documents["level_settings.json"], "level_settings.json"),
        "flows": _flows(documents["flow_settings.json"], "flow_settings.json"),
        "temperatures": temperatures,
        "valve": _valve(documents["valve_settings.json"], "valve_settings.json"),
        "startup": _startup(documents["startup_settings.json"],
                            "startup_settings.json"),
        "drain": _drain(documents["drain_settings.json"], "drain_settings.json"),
        "control": _control(documents["control_settings.json"]),
        "arbiter": _arbiter(documents["arbiter_settings.json"],
                            "arbiter_settings.json"),
        "level_roles": level_roles,
        "command_outputs": command_outputs,
        "io_mapping": io_mapping,
    }
    _check_cross_references(parts)

    return PlantConfig(
        directory=directory,
        temperature_ranges_validated=temperature_ranges_validated,
        roles=copy.deepcopy(roles),
        treatment=copy.deepcopy(documents["treatment_settings.json"]),
        config_hash=digest.hexdigest(),
        file_hashes=file_hashes,
        files=FILES,
        **{**parts, "io_mapping": copy.deepcopy(io_mapping)},
    )
