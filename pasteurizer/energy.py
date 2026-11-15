"""Solar-based permission for the electric heaters (1 kW, 3 kW, both = 4 kW).

The electric heaters may only run on solar power during the day, never on
the battery at night. Each cycle the controller passes the combined
measurements of both Deye inverters (pasteurizer/solar.py, SolarLink.view())
and the heaters that are on in the last written frame. The result is a
permission per mode; the arbiter (rule 4) removes heater requests without
permission. Nothing here creates a request or bypasses another interlock,
and the biogas heating (heating_circulation) is never gated.

Solar surplus (no double counting): the measured inverter load already
contains the heaters that are on. So

    other loads = combined load - rated power of the heaters on (>= 0)
    surplus     = combined PV  - other loads

A heater that is on therefore does not use up its own surplus, and a
heater that would be added must find the surplus beside what already runs.
Limit: when a heater's own thermostat has cut it out, the load reading no
longer contains it and the surplus is overestimated by its rated power.

Per mode (settings: arbiter_settings.json energy):

    start    surplus >= start_surplus_fraction x mode power (200/600/800 W)
             and SOC >= soc_start_min_percent (30 %), both held for
             start_delay_s (120 s)
    keep     between the limits (hysteresis)
    stop     surplus < stop_surplus_fraction x mode power (150/450/600 W)
             for stop_delay_s (120 s), or SOC <= soc_stop_at_percent (20 %)
             at once

Inverter data: both inverters must be current (one missing = incomplete)
with plausible values. Without valid data no permission is granted; an
existing permission ends communication_timeout_s after the last valid data.
SOC: the lower of the two inverter readings (they share one battery).
Battery power: positive = discharging, negative = charging (shown only).
"""

from dataclasses import asdict, dataclass, field
import math

HEATERS = ("heater_1kw", "heater_3kw")
MODES = {
    "heater_1kw": ("heater_1kw",),
    "heater_3kw": ("heater_3kw",),
    "both": ("heater_1kw", "heater_3kw"),
}
MODE_NAMES = {"heater_1kw": "1 kW", "heater_3kw": "3 kW", "both": "1 kW + 3 kW"}

# Mode states (per mode)
PERMITTED = "PERMITTED"     # may run
STARTING = "STARTING"       # start conditions met, start delay running
STOPPING = "STOPPING"       # permitted, stop delay running (surplus low or data missing)
BLOCKED = "BLOCKED"         # not permitted, reason says why
DISABLED = "DISABLED"       # rule switched off in the settings: always permitted


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def mode_for(heaters) -> str | None:
    """Mode key for a set of heaters that are (to be) on."""
    on = tuple(name for name in HEATERS if name in heaters)
    for key, members in MODES.items():
        if members == on:
            return key
    return None


@dataclass(frozen=True)
class Measurements:
    """Combined values of both inverters, or why they are unusable."""

    valid: bool
    reason: str
    pv_w: float | None = None
    load_w: float | None = None
    battery_w: float | None = None      # + discharging, - charging
    soc_percent: float | None = None    # lower of the two readings
    soc_each: tuple = ()


def measurements(view) -> Measurements:
    """Check a SolarLink.view(): complete, current and plausible, or not valid."""
    if not isinstance(view, dict):
        return Measurements(False, "No inverter data (solar monitoring not running)")
    if not view.get("enabled", False):
        return Measurements(False, view.get("reason") or "Solar monitoring disabled")
    inverters = view.get("inverters") or []
    if not inverters:
        return Measurements(False, "No inverters configured")
    missing = [i.get("name", "?") for i in inverters if not i.get("fresh")]
    if missing:
        states = ", ".join(f"{i.get('name', '?')} {i.get('state', '?')}"
                           for i in inverters if not i.get("fresh"))
        return Measurements(False, f"Inverter data incomplete: {states}")
    totals = view.get("totals") or {}
    if not totals.get("complete"):
        return Measurements(False, "Inverter totals incomplete")
    pv, load, battery = totals.get("pv_w"), totals.get("load_w"), totals.get("battery_w")
    socs = [i.get("reading", {}).get("battery_soc_pct") for i in inverters]
    if not all(_finite(v) for v in (pv, load, battery)):
        return Measurements(False, "Inverter values invalid (PV, load or battery)")
    if pv < 0 or load < 0:
        return Measurements(False, f"Inverter values implausible (PV {pv} W, load {load} W)")
    if not socs or not all(_finite(v) and 0 <= v <= 100 for v in socs):
        return Measurements(False, f"Battery SOC invalid ({socs})")
    return Measurements(True, "", float(pv), float(load), float(battery),
                        float(min(socs)), tuple(socs))


@dataclass(frozen=True)
class ModeStatus:
    mode: str
    power_w: float
    permitted: bool
    state: str
    reason: str                     # why blocked / what is running
    start_need_w: float
    stop_need_w: float
    surplus_w: float | None         # surplus beside other loads
    contribution_percent: float | None   # surplus / mode power
    timer_s: float | None = None    # start or stop delay remaining


@dataclass(frozen=True)
class EnergyStatus:
    enabled: bool
    data_valid: bool
    data_reason: str
    data_age_s: float | None        # since the last valid data (controller clock)
    pv_w: float | None
    load_w: float | None
    other_loads_w: float | None     # load without the heaters that are on
    heaters_on: tuple               # heaters on in the last written frame
    heater_on_w: float              # their rated power
    surplus_w: float | None
    battery_w: float | None
    battery_direction: str | None
    soc_percent: float | None
    soc_each: tuple
    available_heating_w: float      # largest permitted mode (0 / 1000 / 3000 / 4000)
    modes: dict = field(default_factory=dict)       # mode -> ModeStatus

    def permitted(self, heaters) -> bool:
        """May exactly these heaters run together?"""
        key = mode_for(heaters)
        if key is None:
            return True                 # no heater: nothing to permit
        return self.modes[key].permitted

    def reason(self, heaters) -> str:
        key = mode_for(heaters)
        return "" if key is None else self.modes[key].reason

    def to_dict(self) -> dict:
        data = asdict(self)
        data["modes"] = {key: asdict(m) for key, m in self.modes.items()}
        return data


class _Mode:
    def __init__(self, key):
        self.key = key
        self.permitted = False
        self.start_since = None
        self.stop_since = None

    def reset_timers(self):
        self.start_since = None
        self.stop_since = None


class EnergyManager:
    def __init__(self, settings, *, start_time: float):
        self.s = settings
        self.modes = {key: _Mode(key) for key in MODES}
        self.last_valid = None          # controller time of the last valid data
        self._last_now = float(start_time)
        self.status: EnergyStatus | None = None

    def power(self, key) -> float:
        return float(sum(self.s.heater_power_w[name] for name in MODES[key]))

    def update(self, view, heaters_on, now) -> tuple[EnergyStatus, list]:
        """One evaluation. heaters_on: heater -> bool from the last written frame
        (None: unknown, counted as off: the surplus is then not raised by it).

        Returns the status and the permission changes as (kind, data) events.
        """
        if not math.isfinite(now) or now < self._last_now:
            raise ValueError("now must be finite and not move backwards")
        self._last_now = now
        s = self.s
        on = tuple(name for name in HEATERS if (heaters_on or {}).get(name) is True)
        on_w = float(sum(s.heater_power_w[name] for name in on))
        m = measurements(view)
        if m.valid:
            self.last_valid = now
        age = None if self.last_valid is None else round(now - self.last_valid, 3)
        other = surplus = None
        if m.valid:
            other = max(0.0, m.load_w - on_w)
            surplus = m.pv_w - other

        before = {key: mode.permitted for key, mode in self.modes.items()}
        statuses = {}
        for key, mode in self.modes.items():
            statuses[key] = self._mode(mode, m, surplus, age, now)

        events = []
        for key, mode in self.modes.items():
            if mode.permitted != before[key] and s.solar_permission_enabled:
                st = statuses[key]
                events.append(("ENERGY_PERMISSION", {
                    "mode": key, "heaters": list(MODES[key]),
                    "permitted": mode.permitted, "reason": st.reason,
                    "surplus_w": None if surplus is None else round(surplus),
                    "soc_percent": m.soc_percent, "pv_w": m.pv_w, "load_w": m.load_w,
                    "heaters_on": list(on)}))

        available = max([self.power(k) for k, st in statuses.items() if st.permitted],
                        default=0.0)
        direction = None
        if m.battery_w is not None:
            direction = ("IDLE" if abs(m.battery_w) < 20 else
                         "DISCHARGING" if m.battery_w > 0 else "CHARGING")
        self.status = EnergyStatus(
            enabled=s.solar_permission_enabled, data_valid=m.valid, data_reason=m.reason,
            data_age_s=age, pv_w=m.pv_w, load_w=m.load_w, other_loads_w=other,
            heaters_on=on, heater_on_w=on_w, surplus_w=surplus, battery_w=m.battery_w,
            battery_direction=direction, soc_percent=m.soc_percent, soc_each=m.soc_each,
            available_heating_w=available, modes=statuses)
        return self.status, events

    # ------------------------------------------------------------------
    def _mode(self, mode, m, surplus, age, now) -> ModeStatus:
        s = self.s
        power = self.power(mode.key)
        start_need = s.start_surplus_fraction * power
        stop_need = s.stop_surplus_fraction * power
        name = MODE_NAMES[mode.key]

        def result(state, reason, timer=None):
            return ModeStatus(
                mode=mode.key, power_w=power, permitted=mode.permitted, state=state,
                reason=reason, start_need_w=start_need, stop_need_w=stop_need,
                surplus_w=surplus,
                contribution_percent=None if surplus is None else round(100 * surplus / power, 1),
                timer_s=None if timer is None else round(max(0.0, timer), 1))

        if not s.solar_permission_enabled:
            mode.permitted = True
            mode.reset_timers()
            return result(DISABLED, "Solar permission switched off in the settings")

        # No usable inverter data --------------------------------------
        if not m.valid:
            mode.start_since = None
            if not mode.permitted:
                return result(BLOCKED, f"No valid inverter data: {m.reason}")
            waited = now - self.last_valid if self.last_valid is not None else math.inf
            if waited >= s.communication_timeout_s:
                mode.permitted = False
                mode.stop_since = None
                return result(BLOCKED, f"Inverter data missing for "
                              f"{s.communication_timeout_s:g} s: {m.reason}")
            return result(STOPPING, f"Inverter data missing ({m.reason}); off in",
                          s.communication_timeout_s - waited)

        soc = m.soc_percent
        surplus_text = f"Solar surplus {surplus:.0f} W"
        # Permitted: SOC stop at once, surplus stop after the delay ------
        if mode.permitted:
            mode.start_since = None
            if soc <= s.soc_stop_at_percent:
                mode.permitted = False
                mode.stop_since = None
                return result(BLOCKED, f"Battery SOC {soc:.0f} % <= "
                              f"{s.soc_stop_at_percent:g} %: {name} stopped")
            if surplus < stop_need:
                if mode.stop_since is None:
                    mode.stop_since = now
                held = now - mode.stop_since
                if held >= s.stop_delay_s:
                    mode.permitted = False
                    mode.stop_since = None
                    return result(BLOCKED, f"{surplus_text} < {stop_need:.0f} W "
                                  f"({s.stop_surplus_fraction:.0%} of {name}) for "
                                  f"{s.stop_delay_s:g} s: stopped")
                return result(STOPPING, f"{surplus_text} < {stop_need:.0f} W "
                              f"({s.stop_surplus_fraction:.0%} of {name}); off in",
                              s.stop_delay_s - held)
            mode.stop_since = None
            return result(PERMITTED, f"{surplus_text} >= {stop_need:.0f} W, "
                          f"SOC {soc:.0f} %")

        # Not permitted: both start conditions held for the delay ------
        mode.stop_since = None
        problems = []
        if soc < s.soc_start_min_percent:
            problems.append(f"Battery SOC {soc:.0f} % < {s.soc_start_min_percent:g} %")
        if surplus < start_need:
            problems.append(f"{surplus_text} < {start_need:.0f} W "
                            f"({s.start_surplus_fraction:.0%} of {name})")
        if problems:
            mode.start_since = None
            return result(BLOCKED, "; ".join(problems))
        if mode.start_since is None:
            mode.start_since = now
        held = now - mode.start_since
        if held >= s.start_delay_s:
            mode.permitted = True
            mode.start_since = None
            return result(PERMITTED, f"{surplus_text} >= {start_need:.0f} W and "
                          f"SOC {soc:.0f} % for {s.start_delay_s:g} s")
        return result(STARTING, f"{surplus_text} >= {start_need:.0f} W, SOC {soc:.0f} %; "
                      f"permitted in", s.start_delay_s - held)
