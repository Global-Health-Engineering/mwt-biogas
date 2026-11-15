"""Signal events in the log: level changes, sensor validity, flow activity,
the reasons of output requests and the scan cycle that links them."""

from gui import history_api
from pasteurizer.historian.queries import HistoryReader
from scenarios.harness import FAST, Sim, scenario
from scenarios.historian import temp_db


def _pump_on(sim):
    return sim.out("transfer_pump_1")


@scenario(106, "Log links a level change and the pump request it caused (same scan, reasons)")
def s106():
    sim = Sim(params=FAST, bath=70.0)
    switches = sim.config.level_roles
    high, low = switches["line_1_tank_high"], switches["line_1_tank_low"]
    sim.run(20)                                    # past the start-up 3 kW interlock
    initial = {e["data"]["sensor"] for e in sim.events("LEVEL_CHANGED") if e["data"]["initial"]}
    assert set(sim.ctl.inputs.signal_processor.levels) <= initial, initial

    sim.plant.tank[1] = 850.0                      # above the high switch
    assert sim.run(5, until=_pump_on)
    change = [e for e in sim.events("LEVEL_CHANGED") if e["data"]["sensor"] == high][-1]
    assert change["data"]["old"] is False and change["data"]["new"] is True
    request = [e for e in sim.events("OUTPUT_REQUESTED")
               if e["data"]["changes"].get("transfer_pump_1") is True][-1]
    assert request["data"]["cycle"] == change["data"]["cycle"], "same scan cycle"
    assert {"kind": "LEVEL_CHANGED", "sensor": high} in request["data"]["same_cycle_signals"]
    why = request["data"]["reasons"]["transfer_pump_1"]
    assert why["reason"] == "Automatic transfer" and why["blocked_by"] == []
    assert why["previous"]["reason"] == "Waiting for high level", why
    text = history_api.describe("OUTPUT_REQUESTED", request["data"])[1]
    assert "transfer_pump_1=ON (Automatic transfer; before: Waiting for high level)" in text
    assert "same scan: EDB-LS-04 Tank 1 high" in text, text

    sim.plant.tank[1] = 102.0                      # just above the low switch: pumps down
    assert sim.run(60, until=lambda s: not _pump_on(s))
    stop = [e for e in sim.events("OUTPUT_REQUESTED")
            if e["data"]["changes"].get("transfer_pump_1") is False][-1]
    assert stop["data"]["reasons"]["transfer_pump_1"]["blocked_by"] == ["SOURCE_LOW"]
    dropped = [e for e in sim.events("LEVEL_CHANGED") if e["data"]["sensor"] == low][-1]
    assert dropped["data"]["new"] is False and dropped["data"]["cycle"] == stop["data"]["cycle"]

    # A blocked start is explained: the pump stays off while the effluent tank is high.
    sim.plant.tank[1] = 850.0
    sim.plant.faults.forced_switches[switches["effluent_tank_high"]] = True
    sim.run(5)
    assert not _pump_on(sim)
    sim.plant.faults.forced_switches.pop(switches["effluent_tank_high"])
    assert sim.run(5, until=_pump_on)
    start = [e for e in sim.events("OUTPUT_REQUESTED")
             if e["data"]["changes"].get("transfer_pump_1") is True][-1]
    assert start["data"]["reasons"]["transfer_pump_1"]["previous"]["blocked_by"] == \
        ["DESTINATION_HIGH"]
    # Events outside a scan (operator commands between cycles) carry no cycle.
    assert all("cycle" in e["data"] for e in sim.events("LEVEL_CHANGED"))


@scenario(107, "Flow sensor start and stop: confirmed after the hold, volume and average logged")
def s107():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(20)
    sim.plant.tank[1] = 850.0
    assert sim.run(5, until=_pump_on)
    started_at = sim.clock.monotonic()
    assert sim.run(10, until=lambda s: any(e["data"]["sensor"] == "flow_sensor_1"
                                           for e in s.events("FLOW_STARTED")))
    start = [e for e in sim.events("FLOW_STARTED") if e["data"]["sensor"] == "flow_sensor_1"][-1]
    assert abs(start["data"]["l_min"] - 5.0) < 0.3, start["data"]
    assert 2.9 <= sim.clock.monotonic() - started_at <= 5.0, "about the 3 s hold"
    assert len([e for e in sim.events("FLOW_STARTED")
                if e["data"]["sensor"] == "flow_sensor_1"]) == 1, "logged once, not per sample"

    sim.run(60)
    sim.plant.tank[1] = 50.0                       # below low: the pump stops
    assert sim.run(15, until=lambda s: any(e["data"]["sensor"] == "flow_sensor_1"
                                           for e in s.events("FLOW_STOPPED")))
    stop = [e for e in sim.events("FLOW_STOPPED") if e["data"]["sensor"] == "flow_sensor_1"][-1]
    d = stop["data"]
    assert d["complete"] and d["cause"].startswith("Flow below"), d
    assert abs(d["avg_l_min"] - 5.0) < 0.3 and d["peak_l_min"] >= d["avg_l_min"], d
    expected = d["avg_l_min"] * d["duration_s"] / 60.0
    assert abs(d["litres"] - expected) <= 0.1 * expected, (d, expected)
    text = history_api.describe("FLOW_STOPPED", d)[1]
    assert text.startswith("EDB-FR-01 Transfer flow, line 1: flow stopped after"), text

    # A pump run without pulses never registers as flow.
    sim.plant.faults.no_pulses.add("flow_sensor_2")
    sim.plant.tank[2] = 850.0
    sim.run(30)
    assert not any(e["data"]["sensor"] == "flow_sensor_2" for e in sim.events("FLOW_STARTED"))


@scenario(108, "Sensor validity logged in every state, held 2 s; acquisition loss is not per sensor")
def s108():
    sim = Sim(params=FAST, bath=70.0)
    sim.run(10)
    assert not sim.events("SENSOR_VALIDITY"), "valid sensors at start are not logged"
    pt4 = lambda: [e for e in sim.events("SENSOR_VALIDITY") if e["data"]["sensor"] == "PT4_B5"]

    sim.plant.faults.invalid_temperatures.add("PT4_B5")     # 1 s dropout: not logged
    sim.run(1.0)
    sim.plant.faults.invalid_temperatures.discard("PT4_B5")
    sim.run(5)
    assert not pt4()

    sim.plant.faults.invalid_temperatures.add("PT4_B5")
    sim.run(3)
    lost = pt4()
    assert len(lost) == 1 and lost[0]["data"]["valid"] is False and lost[0]["data"]["error"]
    sim.plant.faults.invalid_temperatures.discard("PT4_B5")
    sim.run(3)
    assert [e["data"]["valid"] for e in pt4()] == [False, True]

    # PT3 lost outside treatment: no alarm (by design), but the log shows it.
    sim.plant.faults.invalid_temperatures.add("PT3_B4")
    sim.run(3)
    assert sim.alarm("PT3_INVALID") is None
    assert any(e["data"]["sensor"] == "PT3_B4" and not e["data"]["valid"]
               for e in sim.events("SENSOR_VALIDITY"))
    sim.plant.faults.invalid_temperatures.discard("PT3_B4")
    sim.run(3)

    before = len(sim.events("SENSOR_VALIDITY"))
    sim.plant.faults.read_failure = True                    # everything invalid at once
    sim.run(5)
    sim.plant.faults.read_failure = False
    sim.run(5)
    assert len(sim.events("SENSOR_VALIDITY")) == before, "acquisition loss is one alarm"
    assert not [e for e in sim.events("LEVEL_CHANGED") if not e["data"]["initial"]], \
        "no level noise from the read failure"


@scenario(109, "Log page data: signal events stored, operator view hides them, equipment filter")
def s109():
    db = temp_db()
    sim = Sim(params=FAST, bath=70.0, database=db)
    sim.run(20)
    sim.plant.tank[1] = 850.0
    assert sim.run(5, until=_pump_on)
    sim.run(5)
    rows = history_api.events(db, {"limit": "500"})["rows"]
    kinds = {r["kind"] for r in rows}
    assert {"LEVEL_CHANGED", "FLOW_STARTED", "OUTPUT_REQUESTED"} <= kinds, kinds
    operator = history_api.events(db, {"limit": "500", "exclude_kinds":
                                       ",".join(history_api.SIGNAL_KINDS)})["rows"]
    assert operator and not {r["kind"] for r in operator} & set(history_api.SIGNAL_KINDS)
    switch = history_api.events(db, {"limit": "500", "equipment": "level_switch_4"})["rows"]
    assert switch and {r["kind"] for r in switch} == {"LEVEL_CHANGED"}, switch
    assert switch[0]["description"] == "EDB-LS-04 Tank 1 high: not reached → reached"
    pump = history_api.events(db, {"limit": "500", "equipment": "transfer_pump_1"})["rows"]
    assert any("Automatic transfer" in r["description"] for r in pump)
    with HistoryReader(db) as history:
        page = history.events(kinds=["LEVEL_CHANGED"], exclude_kinds=["LEVEL_CHANGED"])
    assert page.rows == []
