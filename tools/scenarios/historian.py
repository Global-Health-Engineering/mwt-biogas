"""Historian scenarios: persistence, storage failure, overflow, clock, shutdown."""

from pathlib import Path
import tempfile
import time

from pasteurizer.event_log import EventLog
from pasteurizer.historian import Historian, SqliteStore
from pasteurizer.historian.csv_export import export_csv
from pasteurizer.modes import Mode
from scenarios.harness import (
    COMMISSIONING, FAST, OPERATOR, Sim, make_config, scenario, stored_rows,
    to_fallback_treatment,
)


def temp_db() -> Path:
    return Path(tempfile.mkdtemp()) / "historian.sqlite"


def sequences(database, boot_id) -> set:
    return {r["sequence"] for table in ("events", "samples")
            for r in stored_rows(database, table) if r["boot_id"] == boot_id}


def assert_complete(database, log):
    """Every sequence number of this run is stored: nothing lost."""
    assert sequences(database, log.boot_id) == set(range(1, log.sequence + 1))


@scenario(54, "Historian survives restarts; unclean end and config change recorded")
def s54():
    db = temp_db()
    sim = Sim(params=FAST, bath=70.0, database=db)
    sim.run(12)
    assert sim.ctl.shutdown("test stop").records_not_stored == 0
    assert_complete(db, sim.log)

    sim2 = sim.restart(params=FAST)                 # same file, new boot
    sim2.run(12)
    assert [b["boot_id"] for b in stored_rows(db, "boots")] == [sim.log.boot_id,
                                                                  sim2.log.boot_id]
    assert stored_rows(db, "boots")[0]["stop_reason"] == "test stop"
    assert not sim2.events("PREVIOUS_RUN_UNCLEAN") and not sim2.events("CONFIG_CHANGED")
    assert_complete(db, sim2.log)
    assert len(sim2.events("CONTROLLER_START")) == 2  # first run's records still there

    # Power lost (no orderly stop), restarted with one setting changed.
    sim3 = sim2.restart(params=FAST, control={"logging.periodic_interval_s": 2.0})
    sim3.run(1)
    unclean = sim3.events("PREVIOUS_RUN_UNCLEAN")
    assert unclean and unclean[-1]["data"]["boot_id"] == sim2.log.boot_id
    changed = sim3.events("CONFIG_CHANGED")
    assert changed and changed[-1]["data"]["changed_files"] == ["control_settings.json"]


@scenario(55, "Historian disk fails during treatment: yellow only, nothing lost")
def s55():
    db = temp_db()
    sim = Sim(params=FAST, bath=84.0, database=db)
    to_fallback_treatment(sim)
    sim.history.fail = True
    sim.run(30)
    assert sim.state == "FALLBACK_TREATMENT"
    alarm = sim.alarm("LOGGER_FAILURE")
    assert alarm and alarm.level == 1 and sim.status.lights["yellow"]
    assert sim.alarm("STATE_SAVE_FAILED") is None and sim.status.state_saved
    assert len(sim.historian.queue) > 0             # held, not lost
    sim.history.fail = False
    sim.run(6)                                      # next retry after 5 s
    gap = sim.events("LOG_GAP")[-1]["data"]
    assert gap["cause"] == "STORAGE_UNAVAILABLE" and gap["outage_s"] >= 25
    assert gap["dropped_events"] == gap["dropped_samples"] == 0 and gap["error"]
    assert sim.alarm("LOGGER_FAILURE") is None
    assert_complete(db, sim.log)


@scenario(56, "Queue overflow: memory bounded, events kept over samples, drops counted")
def s56():
    sim = Sim(params=FAST, bath=70.0, control={"logging.queue_capacity": 100})
    sim.run(12)
    # A burst larger than the queue between two writer runs.
    for i in range(300):
        sim.log.record("TEST_BURST", i=i)
    assert len(sim.historian.queue) == 100
    sim.run(0.4)
    gap = sim.events("LOG_GAP")[-1]["data"]
    assert gap["cause"] == "QUEUE_OVERFLOW" and gap["dropped_events"] >= 200
    assert sim.alarm("LOGGER_FAILURE") is None

    # Storage down for 60 s: samples may use only half the queue.
    sim.history.fail = True
    sim.run(1)
    for i in range(80):
        sim.log.record("TEST_EVENT", i=i)
    sim.run(60)
    assert len(sim.historian.queue) <= 100 and sim.alarm("LOGGER_FAILURE")
    sim.history.fail = False
    sim.run(6)
    gap = sim.events("LOG_GAP")[-1]["data"]
    assert gap["cause"] == "STORAGE_UNAVAILABLE" and gap["dropped_samples"] > 0
    assert sim.alarm("LOGGER_FAILURE") is None
    stored = {r["sequence"] for r in sim.history.events + sim.history.samples}
    missing = set(range(1, sim.log.sequence + 1)) - stored
    reported = sum(r["data"]["dropped_events"] + r["data"]["dropped_samples"]
                   for r in sim.events("LOG_GAP"))
    assert len(missing) == reported                 # every loss is accounted for
    assert len([r for r in sim.events("TEST_EVENT")]) == 80   # events kept


@scenario(57, "Clock jumps and sync changes recorded; timers and retention protected")
def s57():
    db = temp_db()
    sim = Sim(params=FAST, bath=84.0, database=db)
    sim.clock_sync = False
    sim.run(6)
    sim.wall_offset += 7200
    sim.run(1)
    sim.wall_offset -= 30
    sim.run(1)
    sim.clock_sync = True
    sim.run(6)
    corrections = [round(e["data"]["correction_s"]) for e in sim.events("CLOCK_CORRECTION")]
    assert corrections == [7200, -30]
    changes = [(e["data"]["old"], e["data"]["new"]) for e in sim.events("CLOCK_SYNC_CHANGED")]
    assert changes == [("UNKNOWN", "UNSYNCHRONISED"), ("UNSYNCHRONISED", "SYNCHRONISED")]
    samples = stored_rows(db, "samples")
    assert {s["clock_sync"] for s in samples} == {"UNSYNCHRONISED", "SYNCHRONISED"}
    monotonic = [r["monotonic_s"] for r in sorted(
        stored_rows(db, "events") + samples, key=lambda r: r["sequence"])]
    assert monotonic == sorted(monotonic)           # order intact despite UTC jumps
    # Age-based retention only with a trusted clock (size limit always applies).
    store = SqliteStore(db, sim.config.control.logging)
    far_future = sim.wall() + 400 * 86400
    assert store.prune(far_future, clock_trusted=False)["expired_samples"] == 0
    assert store.prune(far_future, clock_trusted=True)["expired_samples"] == len(samples)
    store.close()


@scenario(58, "Orderly shutdown flushes the queue; failing disk reports what is lost")
def s58():
    db = temp_db()
    sim = Sim(params=FAST, bath=70.0, database=db, background=True)
    sim.run(5)
    for i in range(1000):
        sim.log.record("TEST_EVENT", i=i)
    result = sim.ctl.shutdown("orderly test", timeout_s=10.0)
    assert result.records_not_stored == 0 and result.safe_frame_written and result.state_saved
    events = stored_rows(db, "events")
    assert events[-1]["kind"] == "CONTROLLER_STOP"
    assert len([e for e in events if e["kind"] == "TEST_EVENT"]) == 1000
    assert stored_rows(db, "boots")[-1]["stopped_utc"] is not None
    assert_complete(db, sim.log)

    sim2 = Sim(params=FAST, bath=70.0, background=True)
    sim2.run(1)
    sim2.history.fail = True
    for i in range(10):
        sim2.log.record("TEST_EVENT", i=i)
    started = time.monotonic()
    lost = sim2.ctl.shutdown("disk failing", timeout_s=2.0).records_not_stored
    assert lost >= 11 and time.monotonic() - started < 3.0


@scenario(59, "Slow or failing historian never delays the control scan")
def s59():
    sim = Sim(params=FAST, bath=70.0, background=True)
    sim.history.delay_s = 0.5                       # every write takes 0.5 s
    longest = 0.0
    for step in range(150):
        if step == 75:
            sim.history.fail = True
        started = time.perf_counter()
        sim.run(0.2)
        longest = max(longest, time.perf_counter() - started)
    assert longest < 0.1, f"scan took {longest:.3f} s"
    sim.history.delay_s = 0.0
    sim.history.fail = False
    sim.ctl.shutdown("test end", timeout_s=5.0)


@scenario(60, "Recorded content: transitions, operator actions, model, requests vs writes; CSV")
def s60():
    db = temp_db()
    sim = Sim(params=FAST, bath=84.0, database=db,
              control={"logging.periodic_interval_s": 0.2})
    sim.model(status=sim_status("CHECKING"))
    to_fallback_treatment(sim)
    sim.model(status=sim_status("TRACKING"))
    sim.run(1)
    sim.plant.faults.write_failure = True
    sim.run(0.6)                                    # shorter than the watchdog
    sim.plant.faults.write_failure = False
    sim.run(1)
    assert sim.ctl.commands.reset(OPERATOR).accepted
    sim.plant.mode = Mode.MANUAL
    sim.run(1)
    sim.ctl.commands.manual_request(OPERATOR, "transfer_pump_1", True)
    sim.ctl.commands.manual_request(COMMISSIONING, "transfer_pump_1", True)
    sim.ctl.commands.level_override(COMMISSIONING,
                                    sim.config.level_roles["manhole_water_present"], True)
    sim.run(1)

    kinds = {e["kind"] for e in stored_rows(db, "events")}
    for kind in ("CONTROLLER_START", "STATE_TRANSITION", "MODE_CHANGE", "ALARM_RAISED",
                 "ALARM_RESET", "OPERATOR_ACTION", "MODEL_STATUS", "OUTPUT_REQUESTED",
                 "OUTPUT_WRITTEN", "OUTPUT_WRITE_FAILED", "OUTPUT_WRITE_RECOVERED",
                 "INDICATORS"):
        assert kind in kinds, kind
    actions = [(a["data"]["action"], a["data"]["user"], a["data"]["accepted"])
               for a in sim.events("OPERATOR_ACTION")]
    assert ("MANUAL_REQUEST", "op", False) in actions
    assert ("MANUAL_REQUEST", "comm", True) in actions
    assert ("LEVEL_OVERRIDE", "comm", True) in actions
    statuses = [m["data"]["status"] for m in sim.events("MODEL_STATUS")]
    assert "CHECKING" in statuses and "TRACKING" in statuses

    samples = stored_rows(db, "samples")
    last = samples[-1]["data"]
    assert last["temperatures"]["PT3_B4"]["valid"] is True
    assert {"state", "mode", "model", "requested", "written", "flows", "levels",
            "y4_feedback", "alarms"} <= set(last)
    assert any(s["data"]["written"] is None and not s["data"]["write_ok"] for s in samples)

    out = db.parent / "exports"
    assert export_csv(db, "events", out / "events.csv") == len(stored_rows(db, "events"))
    assert export_csv(db, "samples", out / "samples.csv") == len(samples)
    header = (out / "samples.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "temperatures.PT3_B4.celsius" in header and "written.control_valve_percent" in header


@scenario(61, "Retention and size limit: old samples go first, events kept, usage logged")
def s61():
    settings = make_config(control={"logging.max_database_mb": 0.5}).control.logging
    db = temp_db()
    now = [1.8e9]
    historian = Historian(SqliteStore(db, settings), settings, wall_clock=lambda: now[0])
    log = EventLog(historian, wall_clock=lambda: now[0])
    now[0] -= 100 * 86400                         # records from 100 days ago
    log.record("OLD_EVENT")
    for i in range(200):
        log.sample(i=i, padding="x" * 400)
    now[0] += 100 * 86400
    for i in range(3000):
        log.sample(i=i, padding="x" * 400)
        if i % 100 == 0:
            historian.drain()
    historian.writer.prune_soon()
    historian.drain()
    log.check_health()
    historian.drain()
    store = SqliteStore(db, settings)
    assert store.used_bytes() <= 0.5 * 1048576
    store.close()
    events = stored_rows(db, "events")
    reports = [e["data"] for e in events if e["kind"] == "HISTORIAN_STORAGE"]
    assert sum(r["expired_samples"] for r in reports) == 200
    assert sum(r["size_samples"] for r in reports) > 0
    assert reports[-1]["corrupt_files"] == [] and reports[-1]["corrupt_mb"] == 0
    assert any(e["kind"] == "OLD_EVENT" for e in events)   # events kept 365 days
    remaining = [s["data"]["i"] for s in stored_rows(db, "samples")]
    assert remaining == sorted(remaining) and remaining[-1] == 2999   # newest kept
    historian.close(1.0)


def sim_status(name):
    from pasteurizer.model_interface import ModelStatus
    return ModelStatus[name]


def _stamp(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@scenario(62, "Corrupt database at startup: preserved under a new name, earlier ones kept")
def s62():
    db = temp_db()
    garbage = b"this is not a database " * 200
    db.write_bytes(garbage)
    # Earlier preserved files with the names this run would choose first.
    earlier = {}
    for second in range(int(time.time()) - 1, int(time.time()) + 5):
        path = db.with_name(f"historian.corrupt-{_stamp(second)}.sqlite")
        path.write_bytes(b"EARLIER " + path.name.encode())
        earlier[path] = path.read_bytes()
    sim = Sim(params=FAST, bath=70.0, database=db)
    sim.run(2)
    recovered = sim.events("HISTORIAN_DB_RECOVERED")[-1]["data"]
    preserved = db.with_name(recovered["preserved_as"])
    assert preserved not in earlier and preserved.read_bytes() == garbage
    assert all(path.read_bytes() == content for path, content in earlier.items())
    report = sim.events("HISTORIAN_STORAGE")[-1]["data"]
    assert set(report["corrupt_files"]) == {p.name for p in earlier} | {preserved.name}
    assert report["corrupt_mb"] > 0 and report["database_mb"] > 0
    assert sim.status.historian["corrupt_mb"] == report["corrupt_mb"]
    assert sim.alarm("LOGGER_FAILURE") is None
    assert_complete(db, sim.log)                    # this run fully in the new file


@scenario(63, "Database corrupted during treatment: treatment continues, new file started")
def s63():
    db = temp_db()
    sim = Sim(params=FAST, bath=84.0, database=db)
    to_fallback_treatment(sim)
    sim.history.reset()                             # file damaged while closed
    with open(db, "r+b") as file:
        file.write(b"\x00" * 100)
    queued_before = sim.log.sequence
    sim.run(12)
    assert sim.state == "FALLBACK_TREATMENT" and sim.alarm("LOGGER_FAILURE") is None
    recovered = sim.events("HISTORIAN_DB_RECOVERED")
    assert recovered and db.with_name(recovered[-1]["data"]["preserved_as"]).exists()
    stored = sequences(db, sim.log.boot_id)         # the new file
    assert stored and min(stored) <= queued_before + 1
    assert stored == set(range(min(stored), sim.log.sequence + 1))   # nothing lost since


@scenario(64, "Maintenance and shutdown flushing never delay the scan or the safe frame")
def s64():
    db = temp_db()
    settings = make_config(control={"logging.max_database_mb": 5.0}).control.logging
    filler = SqliteStore(db, settings)               # about 30 MB of old samples
    boot = "0" * 32
    import os
    for chunk in range(30):
        filler.write([{"table": "samples", "boot_id": boot, "sequence": chunk * 1000 + i,
                       "utc": "2027-01-01T00:00:00+00:00", "utc_epoch": 1.8e9,
                       "clock_sync": "UNKNOWN", "monotonic_s": 0.0,
                       "data": {"noise": os.urandom(500).hex()}} for i in range(1000)])
    filler.close()

    sim = Sim(params=FAST, bath=70.0, database=db, background=True,
              control={"logging.max_database_mb": 5.0})
    longest, cycles = 0.0, 0
    deadline = time.monotonic() + 60
    while (cycles < 15 or not sim.historian.status().get("used_mb")) \
            and time.monotonic() < deadline:
        started = time.perf_counter()
        sim.run(0.2)                                # quick_check + pruning run meanwhile
        took = time.perf_counter() - started
        longest = max(longest, took)
        cycles += 1
        time.sleep(max(0.0, 0.2 - took))            # real-time scan, as on the RevPi
    report = sim.historian.status()
    assert report.get("used_mb") is not None and report["used_mb"] <= 5.0, report
    assert cycles > 10 and longest < 0.1, f"scan took {longest:.3f} s"

    # Shutdown with a slow disk: the safe frame goes out first, at once.
    sim.history.delay_s = 1.0
    for i in range(500):
        sim.log.record("TEST_EVENT", i=i)
    times = {}
    write = sim.plant.write

    def timed_write(frame):
        times.setdefault("safe", time.perf_counter())
        return write(frame)
    sim.plant.write = timed_write
    started = time.perf_counter()
    result = sim.ctl.shutdown("test", timeout_s=10.0)
    finished = time.perf_counter()
    assert result.safe_frame_written and times["safe"] - started < 0.05
    assert finished - times["safe"] >= 1.0          # the flush came after, in the thread
    o = sim.plant.outputs
    assert o.light_red and not (o.transfer_pump_1 or o.transfer_pump_2 or o.heater_3kw
                                or o.heater_1kw or o.heating_circulation
                                or o.pasteurizer_isolation_open
                                or o.overflow_valve_1_open or o.overflow_valve_2_open)
    assert result.records_not_stored == 0
    print(f"      longest scan during maintenance: {longest * 1000:.1f} ms over {cycles} "
          f"cycles; safe frame {1000 * (times['safe'] - started):.2f} ms after shutdown()")


@scenario(65, "Shutdown safe frame and its write result logged as output events, before the stop")
def s65():
    db = temp_db()
    sim = Sim(params=FAST, bath=70.0, database=db)
    sim.run(15)
    assert sim.plant.outputs.heating_circulation
    assert sim.ctl.shutdown("orderly test").safe_frame_written
    tail = [(e["kind"], e["data"].get("cause")) for e in stored_rows(db, "events")][-3:]
    assert tail == [("OUTPUT_REQUESTED", "SHUTDOWN"), ("OUTPUT_WRITTEN", "SHUTDOWN"),
                    ("CONTROLLER_STOP", None)], tail
    written = sim.events("OUTPUT_WRITTEN")[-1]["data"]
    assert written["frame"]["heating_circulation"] is False and written["frame"]["light_red"]
    assert written["changes"]["heating_circulation"] is False

    sim2 = sim.restart(params=FAST)                  # the output writer fails at shutdown
    sim2.run(15)
    sim2.plant.faults.write_failure = True
    result = sim2.ctl.shutdown("writer failing")
    assert not result.safe_frame_written
    tail = [(e["kind"], e["data"].get("cause")) for e in stored_rows(db, "events")
            if e["boot_id"] == sim2.log.boot_id][-3:]
    assert tail == [("OUTPUT_REQUESTED", "SHUTDOWN"), ("OUTPUT_WRITE_FAILED", "SHUTDOWN"),
                    ("CONTROLLER_STOP", None)], tail
    assert sim2.events("CONTROLLER_STOP")[-1]["data"]["safe_frame_written"] is False


class _LiveStandIn:
    """Plays the field reader: same signals, but declares LIVE (test only)."""

    data_source = "LIVE"

    def __init__(self, plant):
        self.read = plant.read


class _Undeclared:
    def __init__(self, plant):
        self.read = plant.read


@scenario(66, "CSV exports mark SIMULATED/LIVE data and simulated clock timestamps")
def s66():
    import csv
    import sqlite3
    from pasteurizer.clock import MonotonicClock
    from pasteurizer.controller import PlantController
    from pasteurizer.historian import Historian as H
    from pasteurizer.persistence import MemoryStore

    db = temp_db()
    sim = Sim(params=FAST, bath=70.0, database=db)
    sim.run(12)
    sim.ctl.shutdown("simulation done")
    out = db.parent / "exports"
    for table in ("events", "samples"):
        export_csv(db, table, out / f"{table}.csv")
        with open(out / f"{table}.csv", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        assert rows and list(rows[0])[:2] == ["source", "clock_source"]
        assert {(r["source"], r["clock_source"]) for r in rows} == {("SIMULATED", "SIMULATED")}

    # Field-type reader on the system clocks: LIVE / SYSTEM (outputs stay simulated).
    live_db = temp_db()
    config = make_config()
    store = SqliteStore(live_db, config.control.logging)
    log = EventLog(H(store, config.control.logging))
    ctl = PlantController(config, reader=_LiveStandIn(sim.plant), writer=sim.plant,
                          store=MemoryStore(), event_log=log, clock=MonotonicClock())
    ctl.cycle()
    ctl.shutdown("live check")
    export_csv(live_db, "events", out / "live.csv")
    with open(out / "live.csv", encoding="utf-8") as file:
        assert {(r["source"], r["clock_source"]) for r in csv.DictReader(file)} == \
            {("LIVE", "SYSTEM")}

    # A reader that does not declare its source is refused.
    try:
        PlantController(config, reader=_Undeclared(sim.plant), writer=sim.plant,
                        store=MemoryStore(), event_log=EventLog(H(store, config.control.logging)))
        raise AssertionError("undeclared reader accepted")
    except ValueError:
        pass

    # A database from before the source columns: exported as UNKNOWN, then migrated.
    old_db = temp_db()
    old = sqlite3.connect(old_db)
    old.executescript(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, boot_id TEXT NOT NULL, "
        "sequence INTEGER NOT NULL, utc TEXT NOT NULL, utc_epoch REAL NOT NULL, "
        "clock_sync TEXT NOT NULL, monotonic_s REAL NOT NULL, kind TEXT NOT NULL, "
        "data TEXT NOT NULL, UNIQUE (boot_id, sequence));"
        "INSERT INTO events VALUES (1, 'old', 1, '2026-10-30T10:00:00+00:00', 1.79e9, "
        "'UNKNOWN', 1.0, 'CONTROLLER_START', '{}');")
    old.commit()
    old.close()
    export_csv(old_db, "events", out / "old.csv")
    with open(out / "old.csv", encoding="utf-8") as file:
        assert next(csv.DictReader(file))["source"] == "UNKNOWN"
    sim3 = Sim(params=FAST, bath=70.0, database=old_db)
    sim3.run(1)
    sources = [(e["boot_id"], e["source"]) for e in stored_rows(old_db, "events")]
    assert sources[0] == ("old", "UNKNOWN") and sources[-1][1] == "SIMULATED"


@scenario(67, "Database recovery: acknowledgeable yellow warning, kept over restart, files kept")
def s67():
    db = temp_db()
    db.write_bytes(b"not a database " * 100)
    sim = Sim(params=FAST, bath=70.0, database=db)
    sim.run(1)
    alarm = sim.alarm("HISTORIAN_DB_RECOVERED")
    assert alarm and alarm.level == 1 and sim.status.lights["yellow"]
    preserved = db.with_name(sim.events("HISTORIAN_DB_RECOVERED")[-1]["data"]["preserved_as"])
    sim.run(5)
    assert sim.alarm("HISTORIAN_DB_RECOVERED")            # does not clear by itself

    sim2 = sim.restart(params=FAST)                         # not yet acknowledged
    sim2.run(1)
    assert sim2.alarm("HISTORIAN_DB_RECOVERED")
    assert sim2.ctl.commands.acknowledge(OPERATOR, "HISTORIAN_DB_RECOVERED").accepted
    sim2.run(0.4)
    assert sim2.alarm("HISTORIAN_DB_RECOVERED") is None and preserved.exists()
    ack = sim2.events("HISTORIAN_RECOVERY_ACKNOWLEDGED")[-1]["data"]
    assert ack["user"] == "op" and ack["preserved_files"] == [preserved.name]
    assert ack["count"] == 1 and ack["recoveries"][0]["preserved_as"] == preserved.name
    assert sim2.status.historian["corrupt_files"] == [preserved.name]

    sim3 = sim2.restart(params=FAST)                        # acknowledged: stays clear
    sim3.run(1)
    assert sim3.alarm("HISTORIAN_DB_RECOVERED") is None and preserved.exists()


@scenario(68, "Recovery acknowledgement: never clears LOGGER_FAILURE, new recovery needs a new one, lists all")
def s68():
    db = temp_db()
    db.write_bytes(b"not a database " * 100)
    sim = Sim(params=FAST, bath=70.0, database=db)
    sim.run(1)
    assert sim.alarm("HISTORIAN_DB_RECOVERED")

    # Storage fails while the recovery warning is pending.
    sim.history.fail = True
    sim.run(7)
    assert sim.alarm("LOGGER_FAILURE") and sim.alarm("HISTORIAN_DB_RECOVERED")
    assert sim.ctl.commands.acknowledge(OPERATOR, "HISTORIAN_DB_RECOVERED").accepted
    sim.run(1)
    assert sim.alarm("HISTORIAN_DB_RECOVERED") is None
    logger = sim.alarm("LOGGER_FAILURE")
    assert logger and logger.active and not logger.acknowledged   # untouched
    sim.history.fail = False
    sim.run(7)
    assert sim.alarm("LOGGER_FAILURE") is None    # clears only when storage works

    # A second corruption after the acknowledgement: a new warning to acknowledge.
    first = sim.events("HISTORIAN_DB_RECOVERED")[-1]["data"]["preserved_as"]
    sim.history.reset()
    with open(db, "r+b") as file:
        file.write(b"\x00" * 100)
    sim.run(6)
    alarm = sim.alarm("HISTORIAN_DB_RECOVERED")
    assert alarm and alarm.active and not alarm.acknowledged
    second = sim.events("HISTORIAN_DB_RECOVERED")[-1]["data"]["preserved_as"]
    assert second != first
    assert db.with_name(first).exists() and db.with_name(second).exists()
    sim.history.reset()                              # the old process ends
    sim2 = sim.restart(params=FAST)                  # still required after a restart
    sim2.run(1)
    assert sim2.alarm("HISTORIAN_DB_RECOVERED")
    assert sim2.ctl.commands.acknowledge(OPERATOR, "HISTORIAN_DB_RECOVERED").accepted
    sim2.run(0.4)
    assert sim2.alarm("HISTORIAN_DB_RECOVERED") is None
    assert sim2.events("HISTORIAN_RECOVERY_ACKNOWLEDGED")[-1]["data"]["preserved_files"] == [second]

    # Two more corruptions before anyone acknowledges, then a restart: one
    # acknowledgement covers both and lists each event and file.
    names = []
    for _ in range(2):
        sim2.history.reset()
        with open(db, "r+b") as file:
            file.write(b"\x00" * 100)
        sim2.run(6.4)          # next sample (<= 5 s) hits the damage; event next cycle
        names.append(sim2.events("HISTORIAN_DB_RECOVERED")[-1]["data"]["preserved_as"])
    assert len(set(names)) == 2, names
    assert sim2.alarm("HISTORIAN_DB_RECOVERED"), [a.code for a in sim2.status.alarms]
    sim2.history.reset()
    sim3 = sim2.restart(params=FAST)
    sim3.run(1)
    assert sim3.alarm("HISTORIAN_DB_RECOVERED")
    assert sim3.ctl.commands.acknowledge(OPERATOR, "HISTORIAN_DB_RECOVERED").accepted
    sim3.run(0.4)
    ack = sim3.events("HISTORIAN_RECOVERY_ACKNOWLEDGED")[-1]["data"]
    assert ack["preserved_files"] == names and ack["count"] == 2
    assert all({"utc", "boot_id", "sequence", "preserved_as", "error"} <= set(r)
               for r in ack["recoveries"])
    assert sim3.alarm("HISTORIAN_DB_RECOVERED") is None
    assert all(db.with_name(n).exists() for n in [first, second] + names)
