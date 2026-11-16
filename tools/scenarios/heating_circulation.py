"""AUTO heating: circulation pulses in a cold start and the biogas heat diagnostic.

PREHEAT_3KW runs the 3 kW heater and the heating-water circulation together:
pulses on PT6 - PT5, after the maximum idle time or to refresh a PT5 at the
preheat limit; minimum ON/OFF times and a start-to-start interval (burner
ignition calls) on the written pump output. The 64 °C HEAT_UP check, also
from WAITING_FOR_SOLAR, counts only a PT5 refreshed by circulation. HEAT_UP circulates continuously; the biogas heat diagnostic
(Checking / detected / not detected) raises a non-blocking warning.

The simulator has one bath temperature: PT6 reads 3 K above PT5 while the
circulation runs and the burner has gas. Stratification and hot pockets are
injected as sensor offsets. All numbers are PRETEND values.
"""

from pasteurizer.heating import (
    CHECKING,
    DETECTED,
    NOT_DETECTED,
    BiogasHeatMonitor,
    OutputTimer,
    PreheatCirculation,
)
from dataclasses import replace

from scenarios.harness import (
    CYCLE,
    FAST,
    MODEL_ON,
    OPERATOR,
    Sim,
    make_config,
    scenario,
    state_is,
)

# Biogas diagnostic shortened for the simulator (provisional defaults are longer).
QUICK_BIOGAS = {"biogas_heat.startup_delay_s": 20.0, "biogas_heat.hold_s": 10.0,
                "biogas_heat.observation_s": 60.0, "biogas_heat.loss_s": 30.0}


def _edges(sim, seconds, name="heating_circulation"):
    """Run and return (time, on) for every change of a written output."""
    edges, last = [], sim.out(name)
    for _ in range(round(seconds / CYCLE)):
        sim.run(CYCLE)
        now = sim.clock.monotonic()
        value = sim.out(name)
        if value != last:
            edges.append((now, value))
            last = value
    return edges


def _cold(control=None, bath=40.0, **kwargs):
    """Cold start; the bath barely warms so preheat lasts for the test."""
    return Sim(params=dict(bath_heat_capacity_j_k=5e7), bath=bath,
               control=control, **kwargs)


def _starts(edges):
    return [t for t, on in edges if on]


NO_INTERVAL = {"preheat_circulation.min_start_interval_s": 0.0}


def _preheating(sim):
    assert sim.run(15, until=lambda s: s.out("heater_3kw")), sim.status.arbitration_notes
    assert sim.state == "PREHEAT_3KW"


def _biogas(sim):
    return sim.status.heating["biogas_heat"]


@scenario(127, "PREHEAT_3KW: 3 kW and circulation written on together; other preheat exclusions hold")
def s127():
    sim = _cold()
    sim.plant.tank[1] = 850.0                  # transfer 1 would start
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    assert sim.run(30, until=lambda s: s.out("heating_circulation"))
    assert sim.out("heater_3kw") and sim.out("heating_circulation")
    # Transfer pumps keep their 3 kW interlock, the 1 kW its exclusion.
    assert not sim.out("transfer_pump_1") and not sim.out("heater_1kw")
    rules = {(n["output"], n["rule"]) for n in sim.status.arbitration_notes}
    assert not [r for o, r in rules if o == "heating_circulation"], rules
    # The 3 kW keeps its solar permission while the pump runs.
    assert sim.status.energy["status"]["modes"]["heater_3kw"]["permitted"]
    pulse = sim.status.heating["preheat_circulation"]
    assert pulse["requested"] and pulse["trigger"] == "DELTA", pulse


@scenario(128, "Pulse on PT6 - PT5 >= 5 °C: minimum ON 20 s, then the full 20 s OFF (start interval off)")
def s128():
    sim = _cold(control=NO_INTERVAL)
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 4.0     # below the trigger
    sim.run(30)
    assert not sim.out("heating_circulation"), "4 °C must not start a pulse"
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0     # trigger held
    edges = _edges(sim, 130)
    starts = [t for t, on in edges if on]
    stops = [t for t, on in edges if not on]
    assert len(starts) >= 3 and len(stops) >= 2, edges
    for start, stop in zip(starts, stops):
        assert 20.0 <= stop - start <= 20.0 + 3 * CYCLE, (start, stop)
    for stop, start in zip(stops, starts[1:]):
        assert 20.0 <= start - stop <= 20.0 + 3 * CYCLE, (stop, start)
    assert sim.out("heater_3kw") and sim.state == "PREHEAT_3KW"


@scenario(129, "Mixing pulse after the maximum time without circulation, with no trigger (start interval off)")
def s129():
    sim = _cold(control={"preheat_circulation.max_idle_s": 90.0, **NO_INTERVAL})
    sim.run(1)
    entered = sim.clock.monotonic()
    edges = _edges(sim, 230)
    starts = [t for t, on in edges if on]
    stops = [t for t, on in edges if not on]
    assert len(starts) == 2 and len(stops) >= 1, edges
    assert 88.0 <= starts[0] - entered <= 92.0, starts[0] - entered
    assert 20.0 <= stops[0] - starts[0] <= 20.0 + 3 * CYCLE
    assert 90.0 <= starts[1] - stops[0] <= 90.0 + 3 * CYCLE, "idle counted from the pulse end"
    assert sim.status.heating["preheat_circulation"]["trigger"] in ("MAX_IDLE", None)
    assert sim.events("OUTPUT_REQUESTED")[-1]["data"]["reasons"]["heating_circulation"]


@scenario(130, "64 °C to HEAT_UP only on a PT5 refreshed by circulation; a stagnant hot reading does not count")
def s130():
    sim = _cold(control={"preheat_circulation.max_idle_s": 60.0})
    _preheating(sim)
    sim.plant.bath_c = 61.0
    offsets = sim.plant.faults.temperature_offsets
    # Hot pocket at PT5 while the water stands; the pulse shows the mixed value.
    for _ in range(round(150 / CYCLE)):
        offsets["PT5_B6"] = 0.0 if sim.out("heating_circulation") else 4.0
        sim.run(CYCLE)
        assert sim.state == "PREHEAT_3KW", "stagnant PT5 65 °C must not end preheat"
    offsets.pop("PT5_B6", None)
    sim.plant.bath_c = 64.5
    # The stagnant 65 °C reading paused the 3 kW (waiting for a mixed reading).
    assert sim.status.heating["preheat_circulation"]["heater_3kw_hold"]
    # The next refresh pulse waits for the 300 s start interval.
    assert sim.run(400, until=state_is("HEAT_UP")), sim.status.state_reason
    transition = [e for e in sim.events("STATE_TRANSITION")
                  if e["data"]["new"] == "HEAT_UP"][-1]["data"]
    assert "after circulation" in transition["reason"], transition
    # The refreshed reading came from a pulse that ran its minimum time.
    assert sim.ctl.process.circulation.on_for(sim.clock.monotonic()) >= 20.0


@scenario(131, "Pulse timing uses the written pump output; withdrawn permission ends a pulse at once")
def s131():
    s = replace(make_config().control.preheat_circulation, min_start_interval_s=0.0)
    pulse, pump = PreheatCirculation(s, 64.0), OutputTimer()
    pump.update(False, 0.0)
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=0.0) is False, "min OFF from start"
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=20.0) is True
    # Requested, but the output is not written on yet: the ON time does not run.
    pump.update(False, 30.0)
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=45.0) is True
    pump.update(True, 50.0)
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=69.0) is True
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=70.0) is False, "20 s written on"
    pump.update(True, 70.2)                    # still written on this cycle
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=70.2) is False
    pump.update(False, 70.4)
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=90.0) is False, "full OFF time"
    assert pulse.update(40.0, 46.0, permitted=True, heating=True, pump=pump, now=90.4) is True
    pump.update(True, 90.6)
    assert pulse.update(40.0, 46.0, permitted=False, heating=True, pump=pump, now=91.0) is False, \
        "trip / STOP overrides the minimum ON time"
    # Invalid readings never trigger; the idle limit still mixes.
    pulse, pump = PreheatCirculation(s, 64.0), OutputTimer()
    pump.update(False, 0.0)
    assert pulse.update(None, 60.0, permitted=True, heating=True, pump=pump, now=0.0) is False
    assert pulse.update(None, 60.0, permitted=True, heating=True, pump=pump, now=100.0) is False
    assert pulse.update(None, 60.0, permitted=True, heating=True, pump=pump, now=s.max_idle_s) is True
    assert pulse.trigger == "MAX_IDLE"


@scenario(132, "Trips, GUI STOP and the preheat timeout override a running pulse")
def s132():
    # GUI STOP during a pulse: pump and 3 kW off at once.
    sim = _cold()
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    assert sim.run(30, until=lambda s: s.out("heating_circulation"))
    sim.run(2)
    assert sim.ctl.commands.stop(OPERATOR).accepted
    sim.run(0.4)
    assert sim.state in ("STOPPING", "IDLE")
    assert not sim.out("heating_circulation") and not sim.out("heater_3kw")

    # Overtemperature (PT6) during a pulse: TRIPPED, everything off.
    sim = _cold()
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    assert sim.run(30, until=lambda s: s.out("heating_circulation"))
    sim.run(2)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 55.0     # PT6 >= 90 °C
    sim.run(0.4)
    assert sim.state == "TRIPPED" and sim.alarm("OVERTEMPERATURE")
    assert not sim.out("heating_circulation") and not sim.out("heater_3kw")

    # PT5 invalid during a pulse: heating off (no wait for the minimum ON time).
    sim = _cold()
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    assert sim.run(30, until=lambda s: s.out("heating_circulation"))
    sim.plant.faults.invalid_temperatures.add("PT5_B6")
    sim.run(0.4)
    assert not sim.out("heating_circulation") and not sim.out("heater_3kw")

    # Preheat timeout (shortened) while pulses run: trips.
    sim = _cold(control={"preheat.max_duration_s": 40.0})
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    assert sim.run(80, until=state_is("TRIPPED"))
    assert sim.alarm("PREHEAT_TIMEOUT")
    sim.run(0.4)
    assert not sim.out("heating_circulation") and not sim.out("heater_3kw")


@scenario(133, "Solar: no 3 kW permission -> WAITING_FOR_SOLAR, no pulses; permission back -> preheat pulses again")
def s133():
    sim = _cold()
    sim.solar.pv_w = 0.0
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    sim.run(60)
    assert sim.state == "WAITING_FOR_SOLAR", sim.state
    assert not sim.out("heating_circulation") and not sim.out("heater_3kw")
    assert not sim.status.heating["preheat_circulation"]["requested"]
    sim.solar.pv_w = 4000.0
    assert sim.run(120, until=lambda s: s.out("heater_3kw")), sim.status.heating_wait
    assert sim.run(30, until=lambda s: s.out("heating_circulation"))
    assert sim.state == "PREHEAT_3KW"


@scenario(134, "HEAT_UP: continuous circulation; biogas heat Checking, then detected after delay and hold")
def s134():
    sim = Sim(params=FAST, bath=70.0, control=QUICK_BIOGAS)
    sim.run(1)
    assert sim.state in ("HEAT_UP", "FALLBACK_QUALIFY"), sim.state
    assert sim.run(15, until=lambda s: s.out("heating_circulation"))
    assert _biogas(sim)["status"] == CHECKING, _biogas(sim)
    assert _biogas(sim)["label"] == "Checking"
    sim.run(20)                                     # inside startup delay + hold
    assert _biogas(sim)["status"] == CHECKING, _biogas(sim)
    assert sim.run(15, until=lambda s: _biogas(s)["status"] == DETECTED), _biogas(sim)
    assert _biogas(sim)["label"] == "Biogas heat detected"
    assert not sim.alarm("BIOGAS_HEAT_NOT_DETECTED")
    events = [e["data"]["status"] for e in sim.events("BIOGAS_HEAT")]
    assert events[-2:] == [CHECKING, DETECTED], events
    for _ in range(50):
        sim.run(1)
        assert sim.out("heating_circulation"), "circulation stays on continuously"


@scenario(135, "Gas off: not detected after the window, non-blocking warning, circulation and 1 kW control continue")
def s135():
    sim = Sim(params=dict(FAST, biogas_power_w=0.0, bath_loss_w_k=5.0), bath=70.0,
              control=QUICK_BIOGAS)
    assert sim.run(15, until=lambda s: s.out("heating_circulation"))
    sim.run(55)
    assert _biogas(sim)["status"] == CHECKING and not sim.alarm("BIOGAS_HEAT_NOT_DETECTED")
    assert sim.run(10, until=lambda s: s.alarm("BIOGAS_HEAT_NOT_DETECTED")), _biogas(sim)
    alarm = sim.alarm("BIOGAS_HEAT_NOT_DETECTED")
    assert alarm.level == 1 and alarm.message == "Biogas heat not detected", alarm
    assert _biogas(sim)["status"] == NOT_DETECTED
    assert _biogas(sim)["label"] == "Biogas heat not detected"
    sim.run(1)
    assert sim.status.lamp_modes["yellow"] == "steady" and sim.status.lamp_modes["red"] != "steady"
    assert sim.out("heating_circulation"), "circulation stays requested after the warning"
    assert sim.out("heater_1kw"), "1 kW assist keeps its own control"
    assert sim.state not in ("TRIPPED", "IDLE", "STOPPING"), sim.state
    # Warning persists while heat is missing (no flicker), and clears with gas.
    sim.run(60)
    assert sim.alarm("BIOGAS_HEAT_NOT_DETECTED")
    sim.plant.p.biogas_power_w = 8000.0
    assert sim.run(30, until=lambda s: not s.alarm("BIOGAS_HEAT_NOT_DETECTED")), _biogas(sim)
    assert _biogas(sim)["status"] == DETECTED


@scenario(136, "Heat lost after a successful start: warning after the sustained loss, cleared when heat qualifies again")
def s136():
    sim = Sim(params=FAST, bath=70.0, control=QUICK_BIOGAS)
    assert sim.run(60, until=lambda s: _biogas(s)["status"] == DETECTED)
    sim.plant.p.biogas_power_w = 0.0                # gas runs out
    sim.run(25)
    assert _biogas(sim)["status"] == DETECTED and not sim.alarm("BIOGAS_HEAT_NOT_DETECTED")
    assert sim.run(10, until=lambda s: s.alarm("BIOGAS_HEAT_NOT_DETECTED")), _biogas(sim)
    assert _biogas(sim)["status"] == NOT_DETECTED
    assert sim.out("heating_circulation")
    sim.plant.p.biogas_power_w = 8000.0
    assert sim.run(15, until=lambda s: not s.alarm("BIOGAS_HEAT_NOT_DETECTED")), _biogas(sim)
    assert _biogas(sim)["status"] == DETECTED
    kinds = [e["data"]["code"] for e in sim.events("ALARM_RAISED")]
    assert "BIOGAS_HEAT_NOT_DETECTED" in kinds


@scenario(137, "READY and model treatment with the biogas warning active: 1 kW alone; PT5 >= 75 °C for 60 s still required")
def s137():
    sim = Sim(params=dict(FAST, biogas_power_w=0.0, bath_loss_w_k=2.0), bath=70.0,
              control={**MODEL_ON, **QUICK_BIOGAS})
    sim.model(achievable_flow_l_min=0.8)
    assert sim.run(90, until=lambda s: s.alarm("BIOGAS_HEAT_NOT_DETECTED")), _biogas(sim)
    assert sim.plant.bath_c < 75.0 and sim.state == "HEAT_UP", (sim.plant.bath_c, sim.state)
    assert sim.out("heater_1kw") and sim.out("heating_circulation")
    assert sim.run(900, until=state_is("READY")), (sim.plant.bath_c, sim.status.state_reason)
    assert sim.status.signals["temperatures"]["PT5_B6"]["celsius"] >= 75.0
    assert sim.alarm("BIOGAS_HEAT_NOT_DETECTED"), "warning active while READY"
    sim.run(50)
    assert sim.state == "READY", "60 s dwell still applies"
    assert sim.run(20, until=state_is("MODEL_TREATMENT")), sim.status.state_reason
    assert sim.alarm("BIOGAS_HEAT_NOT_DETECTED")
    sim.run(15)
    assert sim.out("pasteurizer_isolation_open")


@scenario(138, "Biogas monitor: stop resets qualification, invalid readings never qualify or lose, leaving clears")
def s138():
    s = make_config(control=QUICK_BIOGAS).control.biogas_heat
    m, pump = BiogasHeatMonitor(s), OutputTimer()

    def step(now, pt5=70.0, pt6=73.0, on=True, active=True):
        pump.update(on, now)
        return m.update(active=active, pump=pump, pt5=pt5, pt6=pt6, now=now)

    assert step(0).status == CHECKING
    assert step(25).status == CHECKING                    # delay over, hold starts
    assert step(30, pt6=None).status == CHECKING          # invalid: hold restarts
    assert step(36).status == CHECKING
    assert step(46).status == DETECTED and m.status == DETECTED
    assert step(50, pt6=70.5).warning is None             # loss timer starts
    assert step(70, pt6=None).warning is None             # invalid: loss timer kept
    r = step(81, pt6=70.5)
    assert r.status == NOT_DETECTED and r.warning is True
    r = step(82, on=False)                                # circulation stops
    assert r.status == NOT_DETECTED and r.warning is None, "warning kept, timers reset"
    assert step(83).circulation_s == 0.0                  # new run: delay again
    assert step(100).status == NOT_DETECTED               # still in the startup delay
    assert step(104).status == NOT_DETECTED
    r = step(114)
    assert r.status == DETECTED and r.warning is False
    r = step(115, active=False)                           # left the second stage
    assert r.status is None and r.warning is False and r.label == "Not evaluated"


@scenario(139, "Heating settings: provisional defaults loaded; cross-checks reject inconsistent values")
def s139():
    from pasteurizer.control_settings import ControlSettingsError
    cs = make_config().control
    pc, b = cs.preheat_circulation, cs.biogas_heat
    assert (pc.trigger_delta_c, pc.min_run_s, pc.min_off_s, pc.min_start_interval_s,
            pc.max_idle_s) == (5.0, 20.0, 20.0, 300.0, 600.0)
    assert (b.startup_delay_s, b.delta_min_c, b.hold_s, b.observation_s, b.loss_s) == \
        (120.0, 2.0, 60.0, 900.0, 300.0)
    assert "heating_circulation" not in make_config().arbiter.preheat_excluded_outputs
    for bad in ({"preheat_circulation.max_idle_s": 10.0},
                {"biogas_heat.observation_s": 100.0}):
        try:
            make_config(control=bad)
        except (ControlSettingsError, ValueError):
            continue
        raise AssertionError(f"accepted {bad}")


@scenario(140, "Start interval: at most one pulse start per 300 s from either trigger; minimum OFF still applies")
def s140():
    # Persistent PT6 - PT5 >= 5 °C: starts 300 s apart, not every 40 s.
    sim = _cold()
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    edges = _edges(sim, 1250)
    starts = _starts(edges)
    assert len(starts) == 5, starts                 # 1250 s / 300 s, first at once
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(300.0 <= g <= 300.0 + 3 * CYCLE for g in gaps), gaps
    held = sim.status.heating["preheat_circulation"]
    assert held["next_start_allowed_in_s"] is not None, held
    # Mixing trigger faster than the interval (max idle 60 s): still 300 s.
    sim = _cold(control={"preheat_circulation.max_idle_s": 60.0})
    sim.run(1)
    gaps = [b - a for a, b in zip(*(lambda st: (st, st[1:]))(_starts(_edges(sim, 1000))))]
    assert len(gaps) >= 2 and all(g >= 300.0 for g in gaps), gaps
    # Interval shorter than ON + OFF: the 20 s minimum OFF still decides.
    s = replace(make_config().control.preheat_circulation, min_start_interval_s=25.0)
    pulse, pump = PreheatCirculation(s, 64.0), OutputTimer()
    pump.update(True, 0.0)
    assert pulse.start_wait(pump, 0.0) == 20.0
    pump.update(False, 20.0)
    assert pulse.start_wait(pump, 30.0) == 10.0 and pulse.start_wait(pump, 40.0) == 0.0


@scenario(141, "Solar loss mid-pulse: the pulse finishes, no restart until the start interval; repeated losses do not cycle the burner")
def s141():
    sim = _cold()
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    assert sim.run(30, until=lambda s: s.out("heating_circulation"))
    started = sim.clock.monotonic()
    # Battery SOC cutoff: the 3 kW loses its permission at once (no retry delay).
    sim.solar.soc_percent = 45.0
    assert sim.run(10, until=state_is("WAITING_FOR_SOLAR")), sim.state
    assert not sim.out("heater_3kw") and sim.out("heating_circulation"), \
        "a solar loss lets the running pulse finish (no short burner cycle)"
    edges = _edges(sim, 25)
    assert edges and not edges[0][1], edges
    assert 20.0 <= edges[0][0] - started <= 20.0 + 3 * CYCLE, edges[0][0] - started
    # Flap the permission: SOC back, cut off again, several times.
    starts = [started]
    for _ in range(4):
        sim.solar.soc_percent = 80.0
        assert sim.run(60, until=state_is("PREHEAT_3KW")), sim.status.heating_wait
        starts += _starts(_edges(sim, 30))
        sim.solar.soc_percent = 45.0
        assert sim.run(10, until=state_is("WAITING_FOR_SOLAR"))
        starts += _starts(_edges(sim, 30))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 300.0 for g in gaps), (starts, gaps)
    span = sim.clock.monotonic() - started
    assert len(starts) <= 1 + span // 300.0, (len(starts), span)
    # A trip still cuts a pulse at once, whatever the state.
    sim.solar.soc_percent = 80.0
    assert sim.run(400, until=lambda s: s.out("heating_circulation"))
    sim.plant.faults.temperature_offsets["PT6_B7"] = 52.0      # PT6 >= 90 °C
    sim.run(0.4)
    assert sim.state == "TRIPPED" and not sim.out("heating_circulation")


@scenario(142, "WAITING_FOR_SOLAR: PT5 reads 64 °C with the pump off, falls below once circulation runs: no HEAT_UP")
def s142():
    sim = _cold(bath=58.0)                          # below the 60 °C cold-start limit
    sim.solar.pv_w = 0.0
    assert sim.run(10, until=state_is("WAITING_FOR_SOLAR")), sim.state
    offsets = sim.plant.faults.temperature_offsets
    pump_starts = []
    for _ in range(round(700 / CYCLE)):
        before = sim.out("heating_circulation")
        offsets["PT5_B6"] = 0.0 if before else 6.5      # stagnant 64.5 °C, mixed 58 °C
        sim.run(CYCLE)
        if sim.out("heating_circulation") and not before:
            pump_starts.append(sim.clock.monotonic())
        assert sim.state == "WAITING_FOR_SOLAR", (sim.state, sim.status.state_reason)
        assert not sim.out("heater_3kw")
    # Refresh checks ran (and showed 58 °C), bounded by the start interval.
    assert 2 <= len(pump_starts) <= 3, pump_starts
    assert all(b - a >= 300.0 for a, b in zip(pump_starts, pump_starts[1:])), pump_starts
    reasons = [e["data"]["reasons"]["heating_circulation"]["reason"]
               for e in sim.events("OUTPUT_REQUESTED")
               if "heating_circulation" in e["data"].get("reasons", {})]
    assert any("refresh check" in r for r in reasons), reasons
    assert not [e for e in sim.events("STATE_TRANSITION") if e["data"]["new"] == "HEAT_UP"]
    # Once the water really is at 64 °C, the next refresh pulse completes it.
    offsets.pop("PT5_B6", None)
    sim.plant.bath_c = 64.5
    assert sim.run(400, until=state_is("HEAT_UP")), sim.status.heating
    assert "solar wait" in sim.status.state_reason, sim.status.state_reason


def _heater_off_spans(sim, seconds, hot_offset=None):
    """Run; with hot_offset, PT5 reads that much higher while the pump is off
    (stagnant hot reading). Returns [(off_at, on_at)] of the written 3 kW."""
    offsets = sim.plant.faults.temperature_offsets
    spans, off_at = [], None
    for _ in range(round(seconds / CYCLE)):
        if hot_offset is not None:
            offsets["PT5_B6"] = 0.0 if sim.out("heating_circulation") else hot_offset
        sim.run(CYCLE)
        now, on = sim.clock.monotonic(), sim.out("heater_3kw")
        if not on and off_at is None:
            off_at = now
        elif on and off_at is not None:
            spans.append((off_at, now))
            off_at = None
    if off_at is not None:
        spans.append((off_at, None))
    return spans


@scenario(143, "PT5 at 64 °C while the start interval blocks the refresh: 3 kW paused, resumes if the mixed PT5 < 64 °C")
def s143():
    sim = _cold(bath=58.0)
    sim.plant.tank[1] = 850.0                       # transfer 1 wants to run
    _preheating(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 6.0
    assert sim.run(30, until=lambda s: s.out("heating_circulation"))   # pulse: interval starts
    first_start = sim.clock.monotonic()
    sim.plant.faults.temperature_offsets.pop("PT6_B7")
    assert sim.run(30, until=lambda s: not s.out("heating_circulation"))
    # Stagnant PT5 reads 64.5 °C (mixed 58 °C) while the interval still runs.
    spans = _heater_off_spans(sim, 330, hot_offset=6.5)
    assert spans, "3 kW must pause at the stagnant 64 °C reading"
    off_at, on_at = spans[0]
    assert on_at is not None, "3 kW resumes after the mixed reading"
    # Paused until the refresh pulse (start interval) has run its 20 s.
    assert first_start + 300.0 + 20.0 <= on_at <= first_start + 300.0 + 20.0 + 4 * CYCLE, \
        (on_at - first_start)
    assert sim.state == "PREHEAT_3KW"
    assert not sim.out("transfer_pump_1"), "no transfer start during the pause"
    assert sim.alarm("PREHEAT_TIMEOUT") is None
    reasons = [e["data"]["reasons"]["heater_3kw"]["reason"] for e in sim.events("OUTPUT_REQUESTED")
               if "heater_3kw" in e["data"].get("reasons", {})]
    assert any("waiting for the mixed reading" in r for r in reasons), reasons
    # The solar permission stays through the pause (no new probe, no retry).
    mode = sim.status.energy["status"]["modes"]["heater_3kw"]
    assert mode["permitted"] and mode["phase"] == "RUNNING", mode
    # Water really at 64 °C: pause at the next stagnant reading, then HEAT_UP
    # after the refresh pulse (no resume).
    sim.plant.faults.temperature_offsets.pop("PT5_B6", None)
    sim.plant.bath_c = 64.5
    assert sim.run(30, until=lambda s: not s.out("heater_3kw"))
    assert sim.run(330, until=state_is("HEAT_UP")), sim.status.heating
    assert not sim.out("heater_3kw")


@scenario(144, "PT5 check during the 3 kW solar probe: no pause until the probe has passed")
def s144():
    sim = _cold(bath=58.0)
    assert sim.run(15, until=lambda s: s.out("heater_3kw")), sim.status.arbitration_notes
    phase = sim.status.energy["status"]["modes"]["heater_3kw"]["phase"]
    assert phase in ("ARMED", "SETTLING"), phase
    sim.plant.faults.temperature_offsets["PT5_B6"] = 6.5   # stagnant 64.5 °C in the probe
    sim.run(5)
    assert sim.out("heater_3kw") and sim.status.heating["preheat_circulation"]["heater_3kw_hold"]
    assert sim.status.energy["status"]["modes"]["heater_3kw"]["phase"] in ("SETTLING", "MEASURING")
    assert sim.run(60, until=lambda s: not s.out("heater_3kw"))
    mode = sim.status.energy["status"]["modes"]["heater_3kw"]
    assert mode["phase"] == "RUNNING" and mode["permitted"], mode
    assert not [e for e in sim.events("ENERGY_PROBE") if e["data"]["event"] in ("HEATER_OFF", "FAIL")]
    assert sim.state == "PREHEAT_3KW" and sim.status.heating["preheat_circulation"]["heater_3kw_hold"]
