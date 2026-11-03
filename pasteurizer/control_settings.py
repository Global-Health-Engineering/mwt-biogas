"""Process-control parameters from config/control_settings.json.

Each group is a frozen dataclass. Field metadata declares the allowed range,
so the loader checks every value the same way and names the file and field
on any error. Values are PROVISIONAL unless the file says otherwise; see
section 15 of the control specification.
"""

from dataclasses import MISSING, dataclass, field, fields
import math


class ControlSettingsError(ValueError):
    pass


def num(minimum=None, maximum=None, *, positive=False, optional=False, integer=False):
    return field(metadata={
        "kind": "int" if integer else "number",
        "minimum": minimum,
        "maximum": maximum,
        "positive": positive,
        "optional": optional,
    })


def flag():
    return field(metadata={"kind": "bool"})


@dataclass(frozen=True)
class StartSettings:
    y4_closed_tolerance_percent: float = num(0.0, 20.0)


@dataclass(frozen=True)
class PreheatSettings:
    max_duration_s: float = num(positive=True, maximum=86400.0)


@dataclass(frozen=True)
class ModelSettings:
    target_log: float = num(positive=True, maximum=12.0)
    operating_reserve_log: float = num(0.0, 6.0)
    entry_reserve_log: float = num(0.0, 6.0)
    max_result_age_s: float = num(positive=True, maximum=60.0)
    return_check_s: float = num(0.0, 3600.0)
    ready_pt5_min_c: float = num(0.0, 100.0)
    ready_dwell_s: float = num(0.0, 3600.0)

    @property
    def operating_target_log(self):
        return self.target_log + self.operating_reserve_log

    @property
    def entry_target_log(self):
        return self.target_log + self.entry_reserve_log


@dataclass(frozen=True)
class FlowPISettings:
    kp_percent_per_l_min: float | None = num(0.0, 1000.0, optional=True)
    ki_percent_per_l_min_s: float | None = num(0.0, 1000.0, optional=True)
    max_flow_l_min: float | None = num(positive=True, maximum=100.0, optional=True)
    slew_percent_per_s: float | None = num(positive=True, maximum=100.0, optional=True)
    flow_rate_limit_l_min_per_s: float | None = num(positive=True, maximum=100.0, optional=True)
    tuning_validated: bool = flag()

    @property
    def assigned(self) -> bool:
        return None not in (self.kp_percent_per_l_min, self.ki_percent_per_l_min_s,
                            self.max_flow_l_min, self.slew_percent_per_s,
                            self.flow_rate_limit_l_min_per_s)


@dataclass(frozen=True)
class FallbackSettings:
    entry_pt5_min_c: float = num(0.0, 100.0)
    dwell_s: float = num(0.0, 3600.0)
    pt3_target_c: float = num(0.0, 100.0)
    hold_band_c: float = num(0.0, 10.0)
    step_percent: float = num(positive=True, maximum=100.0)
    step_interval_s: float = num(positive=True, maximum=600.0)
    start_opening_percent: float = num(0.0, 100.0)
    min_opening_percent: float = num(0.0, 100.0)
    max_opening_percent: float = num(0.0, 100.0)
    max_flow_l_min: float | None = num(positive=True, maximum=100.0, optional=True)
    max_flow_validated_for_hardware: bool = flag()


@dataclass(frozen=True)
class TreatmentStopSettings:
    pt3_min_c: float = num(0.0, 100.0)
    pt3_confirm_s: float = num(0.0, 600.0)
    min_flow_l_min: float = num(0.0, 100.0)
    startup_grace_s: float = num(0.0, 600.0)
    supply_wait_s: float = num(0.0, 3600.0)
    retry_wait_s: float = num(0.0, 3600.0)
    retry_grace_s: float = num(0.0, 600.0)
    retry_restore_s: float = num(0.0, 86400.0)
    interruption_count: int = num(1, 100, integer=True)
    interruption_window_s: float = num(positive=True, maximum=86400.0)


@dataclass(frozen=True)
class Heater1kWSettings:
    normal_on_below_c: float = num(0.0, 100.0)
    normal_off_at_c: float = num(0.0, 100.0)
    fallback_on_below_c: float = num(0.0, 100.0)
    fallback_off_at_c: float = num(0.0, 100.0)
    min_on_s: float = num(0.0, 3600.0)
    min_off_s: float = num(0.0, 3600.0)


@dataclass(frozen=True)
class FaultSettings:
    overtemperature_trip_c: float = num(0.0, 100.0)
    overtemperature_reset_below_c: float = num(0.0, 100.0)
    pt3_invalid_s: float = num(0.0, 600.0)
    pt5_pt6_invalid_s: float = num(0.0, 600.0)
    flow3_invalid_s: float = num(0.0, 600.0)
    selector_invalid_s: float = num(0.0, 600.0)
    transfer_flow_invalid_s: float = num(0.0, 600.0)


@dataclass(frozen=True)
class TransferFlowSettings:
    no_flow_max_l_min: float = num(0.0, 100.0)
    low_flow_below_l_min: float = num(0.0, 100.0)
    grace_s: float = num(0.0, 600.0)
    persistence_s: float = num(0.0, 600.0)


@dataclass(frozen=True)
class OverflowSettings:
    min_open_s: float = num(0.0, 3600.0)
    close_after_clear_s: float = num(0.0, 3600.0)


@dataclass(frozen=True)
class ManualSettings:
    request_expiry_s: float = num(positive=True, maximum=3600.0)
    override_expiry_s: float = num(positive=True, maximum=3600.0)


@dataclass(frozen=True)
class WatchdogSettings:
    heartbeat_timeout_s: float = num(positive=True, maximum=60.0)


@dataclass(frozen=True)
class LoggingSettings:
    """Historian (event log and periodic process data)."""
    periodic_interval_s: float = num(positive=True, maximum=3600.0)
    event_retention_days: float = num(positive=True, maximum=3650.0)
    sample_retention_days: float = num(positive=True, maximum=3650.0)
    max_database_mb: float = num(positive=True, maximum=100000.0)
    queue_capacity: int = num(minimum=100, maximum=1000000, integer=True)
    write_batch_size: int = num(minimum=1, maximum=10000, integer=True)
    flush_interval_s: float = num(positive=True, maximum=60.0)
    retry_interval_s: float = num(positive=True, maximum=600.0)


@dataclass(frozen=True)
class ControlSettings:
    settings_validated: bool = flag()
    model_control_enabled: bool = flag()
    fallback_enabled: bool = flag()
    start: StartSettings = field(default=MISSING)
    preheat: PreheatSettings = field(default=MISSING)
    model: ModelSettings = field(default=MISSING)
    flow_pi: FlowPISettings = field(default=MISSING)
    fallback: FallbackSettings = field(default=MISSING)
    treatment_stop: TreatmentStopSettings = field(default=MISSING)
    heater_1kw: Heater1kWSettings = field(default=MISSING)
    faults: FaultSettings = field(default=MISSING)
    transfer_flow: TransferFlowSettings = field(default=MISSING)
    overflow: OverflowSettings = field(default=MISSING)
    manual: ManualSettings = field(default=MISSING)
    watchdog: WatchdogSettings = field(default=MISSING)
    logging: LoggingSettings = field(default=MISSING)


GROUPS = {
    "start": StartSettings,
    "preheat": PreheatSettings,
    "model": ModelSettings,
    "flow_pi": FlowPISettings,
    "fallback": FallbackSettings,
    "treatment_stop": TreatmentStopSettings,
    "heater_1kw": Heater1kWSettings,
    "faults": FaultSettings,
    "transfer_flow": TransferFlowSettings,
    "overflow": OverflowSettings,
    "manual": ManualSettings,
    "watchdog": WatchdogSettings,
    "logging": LoggingSettings,
}


def _value(meta, value, where):
    kind = meta["kind"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ControlSettingsError(f"{where}: expected true or false")
        return value
    if value is None:
        if meta["optional"]:
            return None
        raise ControlSettingsError(f"{where}: a value is required")
    if kind == "int":
        if type(value) is not int:
            raise ControlSettingsError(f"{where}: expected an integer")
    elif (not isinstance(value, (int, float)) or isinstance(value, bool)
          or not math.isfinite(value)):
        raise ControlSettingsError(f"{where}: expected a finite number")
    if meta["positive"] and value <= 0:
        raise ControlSettingsError(f"{where}: must be greater than zero")
    if meta["minimum"] is not None and value < meta["minimum"]:
        raise ControlSettingsError(f"{where}: must be at least {meta['minimum']}")
    if meta["maximum"] is not None and value > meta["maximum"]:
        raise ControlSettingsError(f"{where}: must be at most {meta['maximum']}")
    return value if kind == "int" else float(value)


def _group(cls, data, where):
    if not isinstance(data, dict):
        raise ControlSettingsError(f"{where}: expected an object")
    names = [f.name for f in fields(cls)]
    unknown = set(data) - set(names)
    missing = set(names) - set(data)
    if unknown or missing:
        raise ControlSettingsError(
            f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}"
        )
    return cls(**{
        f.name: _value(f.metadata, data[f.name], f"{where}.{f.name}")
        for f in fields(cls)
    })


def parse_control_settings(data, where="control_settings.json") -> ControlSettings:
    if not isinstance(data, dict):
        raise ControlSettingsError(f"{where}: expected an object")
    top = {"settings_validated", "model_control_enabled", "fallback_enabled"}
    unknown = set(data) - top - set(GROUPS)
    missing = (top | set(GROUPS)) - set(data)
    if unknown or missing:
        raise ControlSettingsError(
            f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}"
        )

    groups = {name: _group(cls, data[name], f"{where}.{name}")
              for name, cls in GROUPS.items()}
    settings = ControlSettings(
        **{name: _value({"kind": "bool"}, data[name], f"{where}.{name}") for name in top},
        **groups,
    )
    _cross_check(settings, where)
    return settings


def _cross_check(s: ControlSettings, where):
    h = s.heater_1kw
    if h.normal_on_below_c >= h.normal_off_at_c:
        raise ControlSettingsError(f"{where}.heater_1kw: normal ON must be below OFF")
    if h.fallback_on_below_c >= h.fallback_off_at_c:
        raise ControlSettingsError(f"{where}.heater_1kw: fallback ON must be below OFF")
    f = s.faults
    if f.overtemperature_reset_below_c >= f.overtemperature_trip_c:
        raise ControlSettingsError(f"{where}.faults: reset temperature must be below trip")
    fb = s.fallback
    if not fb.min_opening_percent <= fb.start_opening_percent <= fb.max_opening_percent:
        raise ControlSettingsError(
            f"{where}.fallback: start opening must lie within the opening limits"
        )
    t = s.transfer_flow
    if t.no_flow_max_l_min >= t.low_flow_below_l_min:
        raise ControlSettingsError(
            f"{where}.transfer_flow: no-flow limit must be below the low-flow limit"
        )
    if s.model_control_enabled and not s.flow_pi.assigned:
        raise ControlSettingsError(
            f"{where}: model_control_enabled needs every flow_pi value assigned"
        )
    if s.fallback_enabled and fb.max_flow_l_min is None:
        raise ControlSettingsError(
            f"{where}: fallback_enabled needs fallback.max_flow_l_min"
        )
