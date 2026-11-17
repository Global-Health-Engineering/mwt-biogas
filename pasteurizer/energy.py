"""Solar-based permission for the electric heaters (1 kW or 3 kW, one at a time).

The electric heaters may only run on solar power during the day, never drain
the battery at night. Each cycle the controller passes the combined
measurements of both Deye inverters (pasteurizer/solar.py, SolarLink.view()),
the heaters on in the last written frame, the heat requests and the heaters
an interlock holds off. The result is a permission per heater; the arbiter
(rule 4) removes heater requests without permission. Nothing here creates a
request or bypasses another interlock; the biogas heating
(heating_circulation) is never gated.

Single heater (1 kW or 3 kW): a test-based permission (probe). With a full
battery the inverters curtail PV to what is used, so the surplus measured
with the heater off says little. Instead the heater is switched on briefly
and the solar power that then comes in is measured:

    IDLE       no probe. Starts (-> ARMED) with a heat request, current data
               from both inverters, SOC >= soc_start_min_percent (70 %), PV >=
               probe_min_pv_w (0 W PV, e.g. at night: no test) and no interlock
               holding the heater off.
    ARMED      permitted; waits until the last written frame has the heater on
               (deadline probe_switch_on_timeout_s, covers the switching pause).
    SETTLING   heater written on; probe_settle_s (10 s).
    MEASURING  waits for a reading of EACH inverter requested after the settle
               time (a combined value may still hold an older reading of one
               inverter); deadline probe_measure_timeout_s. Then:
               solar contribution = (PV - other loads) / heater power
                   >= probe_min_solar_contribution (0.70), and
               combined battery discharge (both inverters, + = discharging,
               background loads included) <= probe_max_battery_discharge_w
               (853 W)  -> RUNNING, otherwise -> RETRY.
    RUNNING    permitted. Contribution too low or discharge too high for
               probe_hold_s (120 s) -> RETRY. Inverter data missing for
               communication_timeout_s -> RETRY.
    RETRY      not permitted for probe_retry_delay_s (10 min) after a failed or
               aborted probe (fail, data stale during the probe, a deadline
               missed); then IDLE again (still needs a request and PV).
               A COMMISSIONING user can end the delay early ("Retry now" on
               the Solar tab, force_retry): the heater goes to IDLE on the next
               evaluation and all start conditions above still apply.

    In every permitted phase: SOC <= soc_stop_at_percent (50 %) -> off at
    once (IDLE, a new start needs the start SOC again); heat request ended or
    an interlock holds the heater off -> probe ends (IDLE, no retry delay).

Both heaters together (1 kW + 3 kW): never permitted. There is no combined
rule yet (planned with the combined 4 kW heating); the arbiter keeps the
larger permitted heater. With the preheat exclusion they are never
requested together anyway.

Other loads: the measured inverter load already contains the heaters that
are on, so other loads = combined load - rated power of the heaters on
(>= 0). Limit: a heater cut out by its own thermostat is still subtracted.

Battery readings: register 190 is signed 16 bit, + = discharging (heater
test, Nov 2026); the combined value is the sum of both inverters and exists
only when both are current. Diagnostic only (logged, never decides):
residual = PV + battery - load; outside max(battery_balance_tolerance_w,
battery_balance_tolerance_fraction x load) it is flagged - an inverted sign
would show as about twice the load. Sign confirmed on site for charging and
discharging (Nov 2026).
"""

from dataclasses import asdict, dataclass, field
import math

HEATERS = ("heater_1kw", "heater_3kw")
MODES = {
    "heater_1kw": ("heater_1kw",),
    "heater_3kw": ("heater_3kw",),
}
MODE_NAMES = {"heater_1kw": "1 kW", "heater_3kw": "3 kW"}
TOGETHER_REASON = ("1 kW + 3 kW together not permitted: no combined solar rule yet "
                   "(planned with the 4 kW heating)")

# Mode states (per heater)
PERMITTED = "PERMITTED"     # may run (probe passed, RUNNING)
STOPPING = "STOPPING"       # permitted, off after a delay (values low or data missing)
BLOCKED = "BLOCKED"         # not permitted, reason says why
DISABLED = "DISABLED"       # rule switched off in the settings: always permitted
PROBING = "PROBING"         # single heater: test running (permitted)
RETRY_WAIT = "RETRY_WAIT"   # single heater: not permitted until the retry delay ends

# Probe phases (single heater)
IDLE, ARMED, SETTLING, MEASURING, RUNNING, RETRY = (
    "IDLE", "ARMED", "SETTLING", "MEASURING", "RUNNING", "RETRY")
PROBE_PERMITTED = (ARMED, SETTLING, MEASURING, RUNNING)
# Arbitration rules that are not an interlock for the probe (the energy rule
# itself, and the switching pause the switch-on deadline already covers).
NOT_INTERLOCKS = ("ENERGY_SOLAR", "ENERGY_UNKNOWN", "SWITCHING_PAUSE")


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def pct(fraction) -> str:
    """0.6 -> '60', 0.2333 -> '23.3', 0.7996 -> '80' would hide a fail: one decimal."""
    text = f"{100 * fraction:.1f}"
    return text[:-2] if text.endswith(".0") else text


def clock_text(seconds) -> str:
    seconds = max(0, int(math.ceil(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds >= 60 else f"{seconds} s"


@dataclass(frozen=True)
class Measurements:
    """Combined values of both inverters, or why they are unusable."""

    valid: bool
    reason: str
    pv_w: float | None = None
    load_w: float | None = None
    battery_w: float | None = None      # + discharging, - charging (sum of both)
    soc_percent: float | None = None    # lower of the two readings
    soc_each: tuple = ()
    # Per inverter: (name, monotonic time its reading was requested or None).
    read_at: tuple = ()
    battery_each: tuple = ()            # per inverter battery W, same order


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
    readings = [i.get("reading") or {} for i in inverters]
    socs = [r.get("battery_soc_pct") for r in readings]
    if not all(_finite(v) for v in (pv, load, battery)):
        return Measurements(False, "Inverter values invalid (PV, load or battery)")
    if pv < 0 or load < 0:
        return Measurements(False, f"Inverter values implausible (PV {pv} W, load {load} W)")
    if not socs or not all(_finite(v) and 0 <= v <= 100 for v in socs):
        return Measurements(False, f"Battery SOC invalid ({socs})")
    read_at = tuple((i.get("name", "?"), i.get("read_at") if _finite(i.get("read_at")) else None)
                    for i in inverters)
    battery_each = tuple(r.get("battery_power_w") for r in readings)
    return Measurements(True, "", float(pv), float(load), float(battery),
                        float(min(socs)), tuple(socs), read_at, battery_each)


@dataclass(frozen=True)
class ModeStatus:
    mode: str
    power_w: float
    permitted: bool
    state: str
    reason: str                     # why blocked / what is running / waited for
    start_need_w: float
    stop_need_w: float
    surplus_w: float | None         # surplus beside other loads
    contribution_percent: float | None   # surplus / mode power
    timer_s: float | None = None    # delay or deadline remaining
    phase: str | None = None        # single heater: probe phase
    probe: dict = field(default_factory=dict)   # last probe result / thresholds


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
    available_heating_w: float      # largest permitted heater (0 / 1000 / 3000)
    modes: dict = field(default_factory=dict)       # mode -> ModeStatus
    requests: tuple = ()            # heaters with a heat request this cycle
    waiting: str = ""               # what the electric heating waits for (status line)
    balance_residual_w: float | None = None     # PV + battery - load (diagnostic)
    balance_ok: bool | None = None              # within the tolerance (diagnostic)

    def permitted(self, heaters) -> bool:
        """May exactly these heaters run together? Never both (no combined rule)."""
        on = [name for name in HEATERS if name in heaters]
        if not on:
            return True                 # no heater: nothing to permit
        if len(on) > 1:
            return False
        return self.modes[on[0]].permitted

    def reason(self, heaters) -> str:
        on = [name for name in HEATERS if name in heaters]
        if len(on) > 1:
            return TOGETHER_REASON
        return self.modes[on[0]].reason if on else ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["modes"] = {key: asdict(m) for key, m in self.modes.items()}
        return data


class _Probe:
    """Probe state of one heater."""

    def __init__(self, heater):
        self.heater = heater
        self.phase = IDLE
        self.granted_at = None
        self.on_at = None
        self.measure_from = None
        self.retry_until = None
        self.bad_since = None
        self.last = {}                  # last result for the display and log

    @property
    def permitted(self):
        return self.phase in PROBE_PERMITTED


class EnergyManager:
    def __init__(self, settings, *, start_time: float):
        self.s = settings
        self.probes = {name: _Probe(name) for name in HEATERS}
        self.last_valid = None          # controller time of the last valid data
        self._last_now = float(start_time)
        self.status: EnergyStatus | None = None

    def power(self, key) -> float:
        return float(sum(self.s.heater_power_w[name] for name in MODES[key]))

    def update(self, view, heaters_on, now, *, requests=None,
               interlocks=None) -> tuple[EnergyStatus, list]:
        """One evaluation.

        view        SolarLink.view() (or None)
        heaters_on  heater -> bool from the last written frame (None: unknown,
                    counted as off: the surplus is then not raised by it)
        requests    heater -> bool: heat request this cycle (AUTO cold start or
                    assist, MANUAL ON); None: no request
        interlocks  heater -> "RULE: message" for heaters another arbitration
                    rule held off in the last cycle

        Returns the status and the events as (kind, data).
        """
        if not math.isfinite(now) or now < self._last_now:
            raise ValueError("now must be finite and not move backwards")
        self._last_now = now
        s = self.s
        on = tuple(name for name in HEATERS if (heaters_on or {}).get(name) is True)
        requested = tuple(name for name in HEATERS if (requests or {}).get(name) is True)
        interlocks = interlocks or {}
        on_w = float(sum(s.heater_power_w[name] for name in on))
        m = measurements(view)
        if m.valid:
            self.last_valid = now
        age = None if self.last_valid is None else round(now - self.last_valid, 3)
        other = surplus = residual = balance_ok = None
        if m.valid:
            other = max(0.0, m.load_w - on_w)
            surplus = m.pv_w - other
            residual = m.pv_w + m.battery_w - m.load_w
            balance_ok = abs(residual) <= max(s.battery_balance_tolerance_w,
                                              s.battery_balance_tolerance_fraction * m.load_w)

        before = {key: self._permitted(key) for key in MODES}
        events = []
        ctx = dict(m=m, surplus=surplus, age=age, now=now, on=on, requested=requested,
                   interlocks=interlocks, residual=residual, balance_ok=balance_ok,
                   events=events)
        statuses = {name: self._probe(self.probes[name], **ctx) for name in HEATERS}

        for key in MODES:
            if self._permitted(key) != before[key] and s.solar_permission_enabled:
                st = statuses[key]
                events.append(("ENERGY_PERMISSION", {
                    "mode": key, "heaters": list(MODES[key]),
                    "permitted": self._permitted(key), "reason": st.reason,
                    "surplus_w": None if surplus is None else round(surplus),
                    "soc_percent": m.soc_percent, "pv_w": m.pv_w, "load_w": m.load_w,
                    "battery_w": m.battery_w, "heaters_on": list(on)}))

        available = max([self.power(k) for k, st in statuses.items() if st.permitted],
                        default=0.0)
        direction = None
        if m.battery_w is not None:
            direction = ("IDLE" if abs(m.battery_w) < 20 else
                         "DISCHARGING" if m.battery_w > 0 else "CHARGING")
        waiting = "; ".join(
            f"{MODE_NAMES[name]} heater: {statuses[name].reason}"
            + (f" {clock_text(statuses[name].timer_s)}" if statuses[name].timer_s is not None
               and statuses[name].state != BLOCKED else "")
            for name in HEATERS
            if name in requested and statuses[name].state not in (PERMITTED, DISABLED))
        self.status = EnergyStatus(
            enabled=s.solar_permission_enabled, data_valid=m.valid, data_reason=m.reason,
            data_age_s=age, pv_w=m.pv_w, load_w=m.load_w, other_loads_w=other,
            heaters_on=on, heater_on_w=on_w, surplus_w=surplus, battery_w=m.battery_w,
            battery_direction=direction, soc_percent=m.soc_percent, soc_each=m.soc_each,
            available_heating_w=available, modes=statuses, requests=requested,
            waiting=waiting,
            balance_residual_w=None if residual is None else round(residual),
            balance_ok=balance_ok)
        return self.status, events

    def force_retry(self, now) -> list:
        """End the retry delay of every heater in RETRY (operator command).

        Only the waiting is bridged: the heater goes to IDLE on the next
        update() and a new probe still needs a heat request, current data,
        the start SOC, PV and no interlock. Returns the heaters bridged."""
        bridged = []
        for name in HEATERS:
            p = self.probes[name]
            if p.phase == RETRY and p.retry_until is not None and now < p.retry_until:
                p.retry_until = now
                p.last = {**p.last, "forced_retry_at": now}
                bridged.append(name)
        return bridged

    def _permitted(self, key) -> bool:
        if not self.s.solar_permission_enabled:
            return True
        return self.probes[key].permitted

    # ------------------------------------------------------------------
    def _probe(self, p, *, m, surplus, age, now, on, requested, interlocks,
               residual, balance_ok, events) -> ModeStatus:
        s = self.s
        heater = p.heater
        power = self.power(heater)
        name = MODE_NAMES[heater]
        need_contribution = s.probe_min_solar_contribution
        max_discharge = s.probe_max_battery_discharge_w
        contribution = None if surplus is None else surplus / power
        discharge = m.battery_w            # + discharging (sum of both inverters)
        is_on = heater in on
        req = heater in requested
        interlock = interlocks.get(heater)

        def values():
            return {"pv_w": m.pv_w, "load_w": m.load_w, "battery_w": m.battery_w,
                    "battery_each_w": list(m.battery_each),
                    "soc_percent": m.soc_percent,
                    "surplus_w": None if surplus is None else round(surplus),
                    "solar_contribution": None if contribution is None else round(contribution, 3),
                    "balance_residual_w": None if residual is None else round(residual),
                    "balance_ok": balance_ok,
                    "read_at": [[n, t] for n, t in m.read_at]}

        def log(event, reason, **extra):
            events.append(("ENERGY_PROBE", {"heater": heater, "event": event,
                                            "phase": p.phase, "reason": reason,
                                            **values(), **extra}))

        def end(phase, reason, result):
            """Probe or running permission ends: heater off."""
            log(result, reason)
            p.last = {"result": result, "reason": reason, "at": now, **values()}
            p.phase = phase
            p.granted_at = p.on_at = p.measure_from = p.bad_since = None
            if phase == RETRY:
                p.retry_until = now + s.probe_retry_delay_s

        def status(state, reason, timer=None, blockers=()):
            return ModeStatus(
                mode=heater, power_w=power,
                permitted=p.permitted or not s.solar_permission_enabled, state=state,
                reason=reason, start_need_w=need_contribution * power,
                stop_need_w=need_contribution * power, surplus_w=surplus,
                contribution_percent=None if contribution is None
                else round(100 * contribution, 1),
                timer_s=None if timer is None else round(max(0.0, timer), 1),
                phase=p.phase,
                probe={"min_solar_contribution": need_contribution,
                       "max_battery_discharge_w": max_discharge,
                       "min_pv_w": s.probe_min_pv_w, "last": dict(p.last),
                       # what keeps a probe from starting, the heat request aside
                       "start_blockers": list(blockers)})

        if not s.solar_permission_enabled:
            if p.phase != IDLE:
                p.phase = IDLE
            return status(DISABLED, "Solar permission switched off in the settings")

        # Ends that apply to every permitted phase ----------------------
        if p.permitted:
            if m.valid and m.soc_percent <= s.soc_stop_at_percent:
                end(IDLE, f"Battery SOC {m.soc_percent:.0f} % <= "
                          f"{s.soc_stop_at_percent:g} %: {name} off at once", "SOC_CUTOFF")
            elif not req:
                end(IDLE, "Heat request ended", "REQUEST_ENDED")
            elif interlock:
                end(IDLE, f"Held off by an interlock ({interlock})", "INTERLOCK")

        # Probe phases --------------------------------------------------
        if p.phase in (ARMED, SETTLING, MEASURING) and not m.valid:
            end(RETRY, f"Inverter data not current during the probe: {m.reason}", "STALE")
        if p.phase == ARMED:
            if is_on:
                p.phase, p.on_at = SETTLING, now
                p.measure_from = now + s.probe_settle_s
                log("ON", f"{name} written on; settling {s.probe_settle_s:g} s")
            elif now - p.granted_at >= s.probe_switch_on_timeout_s:
                end(RETRY, f"{name} not switched on within "
                           f"{s.probe_switch_on_timeout_s:g} s", "SWITCH_ON_TIMEOUT")
            else:
                return status(PROBING, f"Probe: waiting for the {name} output to switch on; "
                              "deadline in",
                              p.granted_at + s.probe_switch_on_timeout_s - now)
        if p.phase in (SETTLING, MEASURING) and not is_on:
            end(RETRY, f"{name} output went off during the probe", "HEATER_OFF")
        if p.phase == SETTLING:
            if now < p.measure_from:
                return status(PROBING, f"Probe: {name} on, settling", p.measure_from - now)
            p.phase = MEASURING
        if p.phase == MEASURING:
            late = [n for n, t in m.read_at if t is None or t < p.measure_from]
            if late:
                deadline = p.measure_from + s.probe_measure_timeout_s
                if now >= deadline:
                    end(RETRY, f"No reading from {', '.join(late)} within "
                               f"{s.probe_measure_timeout_s:g} s after settling",
                        "MEASURE_TIMEOUT")
                else:
                    return status(PROBING, f"Probe: waiting for a fresh reading from "
                                  f"{', '.join(late)}; deadline in", deadline - now)
            else:
                low = contribution < need_contribution
                high = discharge > max_discharge
                verdict = (f"solar contribution {pct(contribution)} % "
                           f"({'<' if low else '>='} {pct(need_contribution)} %), "
                           f"battery discharge {discharge:.0f} W "
                           f"({'>' if high else '<='} {max_discharge:.0f} W)")
                if low or high:
                    end(RETRY, f"Probe failed: {verdict}", "FAIL")
                else:
                    p.phase = RUNNING
                    p.bad_since = None
                    p.last = {"result": "PASS", "reason": f"Probe passed: {verdict}",
                              "at": now, **values()}
                    log("PASS", f"Probe passed: {verdict}")

        # Running -------------------------------------------------------
        if p.phase == RUNNING:
            if not m.valid:
                waited = now - self.last_valid if self.last_valid is not None else math.inf
                if waited >= s.communication_timeout_s:
                    end(RETRY, f"Inverter data missing for {s.communication_timeout_s:g} s: "
                               f"{m.reason}", "COMM_TIMEOUT")
                else:
                    return status(STOPPING, f"Inverter data missing ({m.reason}); off in",
                                  s.communication_timeout_s - waited)
            elif not is_on:
                p.bad_since = None          # nothing to judge while it is off
                return status(PERMITTED, f"Probe passed; {name} not on at the moment")
            else:
                low = contribution < need_contribution
                high = discharge > max_discharge
                now_text = (f"solar contribution {pct(contribution)} %, "
                            f"battery discharge {discharge:.0f} W")
                if low or high:
                    if p.bad_since is None:
                        p.bad_since = now
                    held = now - p.bad_since
                    why = " and ".join(
                        ([f"solar contribution {pct(contribution)} % < "
                          f"{pct(need_contribution)} %"] if low else [])
                        + ([f"battery discharge {discharge:.0f} W > {max_discharge:.0f} W"]
                           if high else []))
                    if held >= s.probe_hold_s:
                        end(RETRY, f"{why} for {s.probe_hold_s:g} s: {name} off", "HOLD_FAIL")
                    else:
                        return status(STOPPING, f"{why}; off in", s.probe_hold_s - held)
                else:
                    p.bad_since = None
                    return status(PERMITTED, f"Running on solar: {now_text}")

        # Retry delay ---------------------------------------------------
        if p.phase == RETRY:
            if now < p.retry_until:
                last = p.last.get("reason", "")
                return status(RETRY_WAIT, f"{last}; next probe in", p.retry_until - now)
            p.phase = IDLE
            p.retry_until = None

        # Idle: may a probe start? --------------------------------------
        problems = []
        if not req:
            problems.append("No heat request")
        if not m.valid:
            problems.append(f"No valid inverter data: {m.reason}")
        else:
            if m.soc_percent < s.soc_start_min_percent:
                problems.append(f"Battery SOC {m.soc_percent:.0f} % < "
                                f"{s.soc_start_min_percent:g} %")
            if m.pv_w < s.probe_min_pv_w:
                problems.append(f"PV {m.pv_w:.0f} W < {s.probe_min_pv_w:g} W: no probe")
        if interlock:
            problems.append(f"Held off by an interlock ({interlock})")
        if problems:
            last = p.last.get("reason")
            text = "; ".join(problems)
            if last and p.last.get("result") in ("SOC_CUTOFF",) and req:
                text = f"{text} (last: {last})"
            return status(BLOCKED, text,
                          blockers=[x for x in problems if x != "No heat request"])
        p.phase, p.granted_at = ARMED, now
        log("START", f"Probe started: PV {m.pv_w:.0f} W, SOC {m.soc_percent:.0f} %")
        return status(PROBING, f"Probe: waiting for the {name} output to switch on; "
                      "deadline in", s.probe_switch_on_timeout_s)
