"""Scenarios for the inverter reading (pasteurizer/solar.py) and /api/solar.

No inverter and no network: a fake Modbus reader and a fake clock. The
module is read-only; nothing here touches RevPi I/O.
"""

import json
from pathlib import Path

from gui.link import ControllerLink
from gui.server import GuiServer
from pasteurizer import solar as solar_module
from pasteurizer.solar import (
    REGISTER_COUNT, ReaderUnavailable, SolarLink, _read_holding, check_settings, decode,
    load_settings,
)
from scenarios.gui import request
from scenarios.harness import scenario
from scenarios.historian import temp_db

ROOT = Path(__file__).resolve().parents[2]


def registers(load=400, volt=5210, soc=88, pv1=600, pv2=630, battery=578):
    """Holding registers 178..190 as the inverter returns them (battery as uint16)."""
    r = [0] * REGISTER_COUNT
    r[178 - 178], r[183 - 178], r[184 - 178] = load, volt, soc
    r[186 - 178], r[187 - 178] = pv1, pv2
    r[190 - 178] = battery & 0xFFFF
    return r


class FakeInverters:
    """reader(host, port, device_id, timeout): per host a register list or an exception."""

    def __init__(self):
        self.answers = {}
        self.calls = []

    def __call__(self, host, port, device_id, timeout):
        self.calls.append((host, port, device_id, timeout))
        answer = self.answers.get(host, ConnectionError("no route"))
        if isinstance(answer, Exception):
            raise answer
        return list(answer)


# Fixed test inverters: independent of the site addresses in solar_settings.json.
TEST_SOLAR = {"enabled": True, "poll_s": 5.0, "timeout_s": 3.0, "stale_after_s": 15.0,
              "soc_mismatch_pct": 2, "battery_charge_sign_verified": False,
              "inverters": [{"id": "inverter_1", "name": "Inverter 1", "host": "192.168.1.158"},
                            {"id": "inverter_2", "name": "Inverter 2", "host": "192.168.1.162"}]}


def rig():
    settings = check_settings(TEST_SOLAR)
    fake, t = FakeInverters(), [0.0]
    link = SolarLink(settings, reader=fake, clock=lambda: t[0], wall=lambda: 1.8e9 + t[0])
    return settings, fake, t, link


@scenario(110, "Solar: register decoding, signed battery power, settings checked")
def s110():
    d = decode(registers(load=400, volt=5210, soc=88, pv1=600, pv2=630, battery=578))
    assert d == {"battery_voltage_v": 52.1, "battery_soc_pct": 88, "pv1_power_w": 600,
                 "pv2_power_w": 630, "pv_total_w": 1230, "battery_power_w": 578,
                 "load_power_w": 400}
    assert decode(registers(battery=-1200))["battery_power_w"] == -1200     # 0xFB50
    assert decode(registers(battery=-1))["battery_power_w"] == -1           # 0xFFFF
    assert decode(registers(battery=32767))["battery_power_w"] == 32767
    for bad in ([0] * 12, [0] * 12 + [70000], [0] * 12 + [None]):
        try:
            decode(bad)
            raise AssertionError(f"decoded {bad!r}")
        except ValueError:
            pass
    assert solar_module.battery_direction(578) == "DISCHARGING"
    assert solar_module.battery_direction(-578) == "CHARGING"
    assert solar_module.battery_direction(5) == "IDLE"
    # The shipped settings: both inverters, 5 s poll, charge sign not verified.
    settings = load_settings(ROOT / "config" / "solar_settings.json")
    assert [(i["id"], i["port"], i["device_id"]) for i in settings["inverters"]] == [
        ("inverter_1", 502, 1), ("inverter_2", 502, 1)]
    assert all(i["host"].startswith("192.168.1.") for i in settings["inverters"])
    assert settings["poll_s"] == 5.0 and settings["battery_charge_sign_verified"] is False
    good = {"inverters": [{"id": "a", "name": "A", "host": "10.0.0.1"}]}
    assert check_settings(good)["inverters"][0]["port"] == 502
    for bad in ({"inverters": []}, {**good, "poll_s": 0}, {**good, "timeout_s": 6},
                {**good, "stale_after_s": 1}, {**good, "enabled": "yes"},
                {"inverters": [{"id": "a", "name": "A", "host": "x"}, {"id": "a", "name": "B", "host": "y"}]},
                {"inverters": [{"id": "a", "name": "A", "host": "x", "port": 70000}]}):
        try:
            check_settings(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    # The solar file is part of the controller configuration and its hash.
    from pasteurizer import config as plant_config
    assert "solar_settings.json" in plant_config.FILES        # controller config, hashed


@scenario(111, "Solar: totals over fresh inverters only, never a missing one as zero; stale and offline")
def s111():
    settings, fake, t, link = rig()
    v = link.view()
    assert [i["state"] for i in v["inverters"]] == ["WAITING", "WAITING"]
    assert v["totals"]["pv_w"] is None and not v["totals"]["complete"]
    # Both answer: the heater-test numbers from the validation (heater ON).
    fake.answers = {"192.168.1.158": registers(battery=578, soc=88, load=700),
                    "192.168.1.162": registers(battery=617, soc=88, load=720, pv1=0, pv2=0)}
    link.poll_once()
    assert fake.calls[0] == ("192.168.1.158", 502, 1, 3.0)
    v = link.view()
    assert [i["state"] for i in v["inverters"]] == ["OK", "OK"]
    # read_at: controller time the reading was requested (probe freshness).
    assert [i["read_at"] for i in v["inverters"]] == [0.0, 0.0]
    assert v["totals"]["battery_w"] == 1195 and v["totals"]["battery_direction"] == "DISCHARGING"
    assert v["totals"]["pv_w"] == 1230 and v["totals"]["load_w"] == 1420
    assert v["totals"]["complete"] and v["totals"]["missing"] == []
    assert v["soc_pct"] == {"inverter_1": 88, "inverter_2": 88} and not v["soc_mismatch"]
    assert v["inverters"][0]["last_ok_utc"].startswith("2027-01-15")
    # Inverter 2 stops answering: its last values stay fresh for 15 s, then OFFLINE.
    fake.answers["192.168.1.162"] = TimeoutError("timed out")
    t[0] += 5
    link.poll_once()
    v = link.view()
    inv2 = v["inverters"][1]
    assert inv2["state"] == "OK" and inv2["consecutive_failures"] == 1 and "timed out" in inv2["error"]
    assert v["totals"]["complete"]
    t[0] += 11
    link.poll_once()
    v = link.view()
    inv2 = v["inverters"][1]
    assert inv2["state"] == "OFFLINE" and not inv2["fresh"] and inv2["reading"]["battery_power_w"] == 617
    totals = v["totals"]
    assert not totals["complete"] and totals["missing"] == ["Inverter 2"]
    assert totals["included"] == ["Inverter 1"]
    assert totals["battery_w"] == 578 and totals["load_w"] == 700      # inverter 1 only, not +0
    assert v["soc_pct"]["inverter_2"] == 88                              # last known, flagged by state
    # Both gone: no totals at all (None, not 0).
    fake.answers = {}
    t[0] += 16
    link.poll_once()
    v = link.view()
    assert [i["state"] for i in v["inverters"]] == ["OFFLINE", "OFFLINE"]
    assert v["totals"]["pv_w"] is None and v["totals"]["battery_w"] is None and not v["totals"]["complete"]
    # A poller that stops reading without an error: STALE.
    fake.answers = {"192.168.1.158": registers(), "192.168.1.162": registers(soc=80, battery=-900)}
    link.poll_once()
    v = link.view()
    assert v["totals"]["complete"] and v["soc_mismatch"]                 # 88 vs 80 > 2 %
    assert v["inverters"][1]["reading"]["battery_direction"] == "CHARGING"
    assert v["battery_charge_sign_verified"] is False
    t[0] += 16
    assert [i["state"] for i in link.view()["inverters"]] == ["STALE", "STALE"]
    # Bad data from the inverter counts as a failed read, not as a value.
    fake.answers["192.168.1.158"] = [1, 2, 3]
    link.poll_once()
    inv1 = link.view()["inverters"][0]
    assert inv1["state"] == "OFFLINE" and "ValueError" in inv1["error"]


@scenario(112, "Solar: pymodbus missing or of another version; the GUI still runs")
def s112():
    settings, _, t, _ = rig()

    def no_library(*_):
        raise ReaderUnavailable("pymodbus not installed in this Python")
    link = SolarLink(settings, reader=no_library, clock=lambda: t[0], wall=lambda: 0.0)
    link.poll_once()
    v = link.view()
    assert [i["state"] for i in v["inverters"]] == ["UNAVAILABLE", "UNAVAILABLE"]
    assert "pymodbus" in v["inverters"][0]["error"] and v["totals"]["pv_w"] is None

    class Result:
        registers = registers()

        def isError(self):
            return False

    class Client:
        def __init__(self, accepts):
            self.accepts, self.used = accepts, None

        def read_holding_registers(self, address, *, count, **kw):
            (keyword, value), = kw.items()
            if keyword != self.accepts:
                raise TypeError(f"read_holding_registers() got an unexpected keyword argument '{keyword}'")
            assert (address, count, value) == (178, 13, 7)
            self.used = keyword
            return Result()
    for accepts in ("device_id", "slave", "unit"):
        client = Client(accepts)
        assert decode(_read_holding(client, 7).registers)["pv_total_w"] == 1230
        assert client.used == accepts
    # A TypeError for another reason is not hidden by the version fallback.

    class Broken:
        def read_holding_registers(self, *a, **kw):
            raise TypeError("unsupported operand")
    try:
        _read_holding(Broken(), 1)
        raise AssertionError("TypeError swallowed")
    except TypeError as error:
        assert "operand" in str(error)
    # The module has no write path to the inverters.
    source = Path(solar_module.__file__).read_text()
    assert "write_register" not in source and "write_coil" not in source


@scenario(113, "Solar tab data: /api/solar passes the controller's inverter and permission data, strict JSON")
def s113():
    from dataclasses import asdict
    from pasteurizer.publisher import _clean
    from scenarios.harness import FAST, Sim
    sim = Sim(params=FAST, bath=78.0)
    sim.solar.pv_w = 0.0                     # night: 1 kW requested, blocked
    sim.run(15)
    published = json.loads(json.dumps(_clean(asdict(sim.status)), allow_nan=False, default=str))
    link = ControllerLink("http://controller", fetch=lambda url, timeout: (
        published if url.endswith("/status") else {"ok": True}))
    link.poll_once()
    gui = GuiServer(link, temp_db(), port=0)
    offline = GuiServer(ControllerLink("http://127.0.0.1:9"), temp_db(), port=0)
    gui.start()
    offline.start()
    try:
        code, body = request(gui.port, "/api/solar")
        assert code == 200 and body["controller"]["state"] == "CONNECTED"
        assert [i["state"] for i in body["solar"]["inverters"]] == ["OK", "OK"]
        energy = body["energy"]
        assert energy["data_valid"] and energy["available_heating_w"] == 0
        assert energy["modes"]["heater_1kw"]["state"] == "BLOCKED"
        heater = body["heaters"]["heater_1kw"]
        assert heater["requested"] and not heater["commanded"]
        assert heater["notes"][0]["rule"] == "ENERGY_SOLAR"
        assert {"probe_min_solar_contribution", "probe_max_battery_discharge_w",
                "probe_retry_delay_s"} <= set(body["settings"])
        assert request(gui.port, "/api/solar", "POST")[0] == 405          # read-only
        code, body = request(offline.port, "/api/solar")
        assert code == 200 and body["solar"] is None and body["energy"] is None
        code, page, _ = request(gui.port, "/", raw=True)
        assert code == 200 and 'data-tab="solar"' in page and "/static/solar.js" in page
        code, script, _ = request(gui.port, "/static/solar.js", raw=True)
        assert code == 200 and "/api/solar" in script and "Electrical heating" in script
    finally:
        gui.stop()
        offline.stop()
