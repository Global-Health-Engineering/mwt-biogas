"""Scenarios for the data the read-only GUI uses: status fields and history queries."""

from datetime import datetime
import os
import sqlite3
import time

from pasteurizer.alarm_catalogue import CATALOGUE, PASTEURIZER_TRIP
from pasteurizer.fault_manager import FAULTS
from pasteurizer.historian.queries import HistoryReader
from scenarios.harness import (
    FAST, MODEL_ON, OPERATOR, Sim, make_config, scenario, state_is, to_fallback_treatment,
)
from scenarios.historian import temp_db


@scenario(69, "Status identity, output layers and lamp modes for the GUI")
def s69():
    sim = Sim(params=FAST, bath=84.0)
    sim.run(1)
    st = sim.status
    assert (st.data_source, st.clock_source) == ("SIMULATED", "SIMULATED")
    assert st.boot_id == sim.log.boot_id and st.config_hash == sim.config.config_hash
    assert abs(datetime.fromisoformat(st.utc).timestamp() - sim.wall()) < 0.01
    assert sim.run(300, until=state_is("FALLBACK_QUALIFY", "HEAT_UP"))
    assert sim.status.lamp_modes["green"] == "blinking"
    to_fallback_treatment(sim)
    assert sim.status.lamp_modes == {"green": "steady", "yellow": "off", "red": "blinking"}
    # A failed write: commanded frame published, nothing reported as written.
    sim.plant.faults.write_failure = True
    sim.run(0.4)
    st = sim.status
    assert st.written is None and st.commanded["light_red"] is True
    assert not any(v for k, v in st.commanded.items()
                   if k not in ("light_red", "light_yellow", "control_valve_percent"))
    assert st.lamp_modes["red"] == "steady" and st.requested is not None


@scenario(70, "Model summary: disabled, unusable or usable, never assumed")
def s70():
    sim = Sim(params=FAST, bath=84.0)
    sim.run(1)
    m = sim.status.model
    assert m["enabled"] is False and m["status"] is None and m["usable"] is False
    sim.model(status=_status("TRACKING"), predicted_pt3_c=79.5)
    sim.run(1)
    m = sim.status.model
    assert m["status"] == "TRACKING" and m["usable"] is False   # model control disabled
    assert m["predicted_pt3_c"] == 79.5 and 0 <= m["age_s"] < 1.5
    assert m["measured_pt3_c"] is not None

    sim2 = Sim(params=FAST, bath=84.0, control=MODEL_ON)
    sim2.model(status=_status("TRACKING"))
    sim2.run(2)
    assert sim2.status.model["enabled"] and sim2.status.model["usable"]
    assert sim2.status.model["predicted_log_reduction"] == 6.0


@scenario(71, "Alarm views: severity, raised time, equipment, consequence")
def s71():
    assert set(CATALOGUE) == {f.code for f in FAULTS}
    sim = Sim(params=FAST, bath=70.0)
    sim.run(5)
    before = sim.wall()
    sim.plant.faults.temperature_offsets["PT6_B7"] = 30.0
    sim.run(0.6)
    alarm = sim.alarm("OVERTEMPERATURE")
    assert alarm.severity == "CRITICAL" and alarm.equipment == ("PT5_B6", "PT6_B7")
    assert alarm.consequence == PASTEURIZER_TRIP and alarm.procedure is None
    raised = datetime.fromisoformat(alarm.raised_utc).timestamp()
    assert before <= raised <= sim.wall()
    sim.plant.faults.temperature_offsets["PT6_B7"] = 0.0
    sim.plant.faults.temperature_offsets["PT4_B5"] = 500.0          # implausible: invalid
    sim.run(1)
    assert sim.alarm("PT4_INVALID").severity == "WARNING"


@scenario(72, "History events: newest first, paging, live follow, filters, read-only")
def s72():
    db = temp_db()
    sim = Sim(params=FAST, bath=84.0, database=db)
    to_fallback_treatment(sim)
    sim.plant.faults.temperature_offsets["PT6_B7"] = 8.0      # above the 90 C trip
    sim.run(1)
    assert sim.alarm("OVERTEMPERATURE")
    sim.ctl.commands.reset(OPERATOR)
    sim.run(1)
    with HistoryReader(db) as history:
        first = history.events(limit=5)
        ids = [r["id"] for r in first.rows]
        assert ids == sorted(ids, reverse=True) and len(ids) == 5
        older = history.events(limit=5, before_id=first.next_before_id + 1)
        assert older.rows[0]["id"] < ids[-1] + 1 and not set(ids[:-1]) & {
            r["id"] for r in older.rows}
        assert {r["source"] for r in first.rows} == {"SIMULATED"}
        newest = first.newest_id
        critical = history.events(severity="CRITICAL", limit=50).rows
        assert critical and all(r["kind"].startswith("ALARM_") for r in critical)
        assert {r["data"]["code"] for r in critical} == {"OVERTEMPERATURE"}
        pump = history.events(equipment="heating_circulation", limit=50).rows
        assert pump and all("heating_circulation" in r["data"].get("changes", {})
                            or r["kind"].startswith("ALARM_") for r in pump)
        kinds = history.events(kinds=["STATE_TRANSITION"], limit=50).rows
        assert kinds and {r["kind"] for r in kinds} == {"STATE_TRANSITION"}
        middle = kinds[len(kinds) // 2]["utc"]
        assert all(r["utc"] >= middle for r in history.events(
            kinds=["STATE_TRANSITION"], since=middle, limit=50).rows)
    sim.ctl.commands.stop(OPERATOR)                   # a new event to follow
    sim.run(2)                                        # live follow: only newer rows
    with HistoryReader(db) as history:
        follow = history.events(after_id=newest, limit=100).rows
        assert follow and all(r["id"] > newest for r in follow)
        try:
            history._db.execute("DELETE FROM events")
            raise AssertionError("history reader could write")
        except sqlite3.OperationalError:
            pass


@scenario(73, "Trends: invalid readings and run changes break lines, gaps listed, bounded")
def s73():
    db = temp_db()
    sim = Sim(params=FAST, bath=84.0, database=db, control={"logging.periodic_interval_s": 1.0})
    start = sim.status.utc if sim.status else None
    sim.run(20)
    start = start or sim.status.utc
    sim.plant.faults.invalid_temperatures.add("PT3_B4")
    sim.run(5)
    sim.plant.faults.invalid_temperatures.discard("PT3_B4")
    sim.history.fail = True
    sim.run(7)
    sim.history.fail = False
    sim.run(7)
    sim.history.reset()
    sim2 = sim.restart(params=FAST, control={"logging.periodic_interval_s": 1.0})
    sim2.run(10)
    end = datetime.fromtimestamp(sim2.wall() + 1).astimezone().isoformat()
    with HistoryReader(db) as history:
        result = history.trend(["PT3", "Y4_FEEDBACK", "FLOW3"], since="2000-01-01T00:00:00",
                               until=end)
        pt3 = result["series"]["PT3"]
        assert pt3["units"] == "degC"
        values = [p["value"] for p in pt3["points"]]
        assert None in values and any(v is not None for v in values)
        assert len({p["run"] for p in pt3["points"]}) == 2
        assert result["gaps"] and result["sources"] == [("SIMULATED", "SIMULATED")]
        small = history.trend(["PT3"], since="2000-01-01T00:00:00", until=end, max_points=10)
        assert len(small["series"]["PT3"]["points"]) <= 11 and small["stride"] > 1

    # A large database stays bounded and quick.
    big = temp_db()
    from pasteurizer.historian import SqliteStore
    store = SqliteStore(big, make_config().control.logging)
    for chunk in range(50):
        store.write([{"table": "samples", "source": "SIMULATED", "clock_source": "SIMULATED",
                      "boot_id": "b" * 32, "sequence": chunk * 1000 + i,
                      "utc": "2027-01-01T00:00:00+00:00", "utc_epoch": 1.8e9 + chunk * 1000 + i,
                      "clock_sync": "UNKNOWN", "monotonic_s": float(i),
                      "data": {"temperatures": {"PT3_B4": {"celsius": 80.0, "valid": True}},
                               "noise": os.urandom(100).hex()}} for i in range(1000)])
    store.close()
    started = time.perf_counter()
    with HistoryReader(big) as history:
        result = history.trend(["PT3"], since="2027-01-01T00:00:00+00:00",
                               until="2028-01-01T00:00:00+00:00", max_points=500)
    took = time.perf_counter() - started
    assert len(result["series"]["PT3"]["points"]) <= 501 and took < 2.0, took
    print(f"      50 000 samples -> {len(result['series']['PT3']['points'])} points "
          f"in {took * 1000:.0f} ms")


def _status(name):
    from pasteurizer.model_interface import ModelStatus
    return ModelStatus[name]
