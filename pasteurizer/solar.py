"""Deye hybrid inverters over Modbus TCP: read-only measurements.

Two Deye 5 kW hybrid inverters run in parallel on one battery. The
controller process runs the only Modbus client (run_controller.py starts a
SolarLink); the energy manager (pasteurizer/energy.py) turns its view into
heater permissions and the GUI shows it from the controller's status.
Nothing here switches anything: the module only calls read_holding_registers.

    SolarLink(config.solar).start()

One thread per inverter: a timed-out inverter (3 s) never delays the other,
and the control scan never waits for Modbus (view() only copies the last
results). Every poll connects, reads holding registers 178-190 in one
request and disconnects. Per inverter the view reports:

    WAITING      no read attempted yet
    OK           last good reading younger than stale_after_s (15 s)
    STALE        no good reading for stale_after_s, no read error (poller stuck)
    OFFLINE      no good reading for stale_after_s, reads fail (or never worked)
    UNAVAILABLE  pymodbus is not installed in this Python

Totals (PV, battery, load) are summed over OK inverters only. A missing or
stale inverter is never counted as zero: the totals then say complete=False
and list the missing inverters. SOC is never added; it is reported per
inverter, with a flag when the two differ.

Register map (holding registers, validated with a 1 kW heater test, Nov 2026):
    178 load power W, 183 battery voltage V/100, 184 battery SOC %,
    186 PV1 power W, 187 PV2 power W, 190 battery power W (signed 16 bit).
Battery power: positive = discharging (validated), negative = charging
(confirmed on site, Nov 2026: battery_charge_sign_verified true).
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
import time

FIRST_REGISTER = 178
LAST_REGISTER = 190
REGISTER_COUNT = LAST_REGISTER - FIRST_REGISTER + 1      # 13
STATES = ("WAITING", "OK", "STALE", "OFFLINE", "UNAVAILABLE")
IDLE_BATTERY_W = 20         # |battery power| below this: shown as idle

DEFAULTS = {
    "enabled": True,
    "poll_s": 5.0,
    "timeout_s": 3.0,
    "stale_after_s": 15.0,
    "soc_mismatch_pct": 2,
    "battery_charge_sign_verified": False,
}
INVERTER_DEFAULTS = {"port": 502, "device_id": 1}


class ReaderUnavailable(RuntimeError):
    """The Modbus library cannot be used (not installed)."""


def utc_text(epoch):
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# Settings -------------------------------------------------------------------

def load_settings(path) -> dict:
    """Read and check config/solar_settings.json; ValueError when invalid."""
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"{path.name}: cannot read ({error})") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name}: invalid JSON ({error})") from error
    return check_settings(document, path.name)


def check_settings(document, where="solar settings") -> dict:
    if not isinstance(document, dict):
        raise ValueError(f"{where}: must be an object")
    settings = {**DEFAULTS, **{k: v for k, v in document.items() if not k.startswith("_")}}
    for key in ("poll_s", "timeout_s", "stale_after_s", "soc_mismatch_pct"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{where}: {key} must be a positive number")
    if settings["timeout_s"] >= settings["poll_s"]:
        raise ValueError(f"{where}: timeout_s must be shorter than poll_s")
    if settings["stale_after_s"] < settings["poll_s"]:
        raise ValueError(f"{where}: stale_after_s must be at least poll_s")
    for key in ("enabled", "battery_charge_sign_verified"):
        if not isinstance(settings[key], bool):
            raise ValueError(f"{where}: {key} must be true or false")
    inverters = settings.get("inverters")
    if not isinstance(inverters, list) or not inverters:
        raise ValueError(f"{where}: inverters must be a non-empty list")
    checked, ids = [], set()
    for n, entry in enumerate(inverters):
        at = f"{where}: inverters[{n}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{at} must be an object")
        inv = {**INVERTER_DEFAULTS, **{k: v for k, v in entry.items() if not k.startswith("_")}}
        for key in ("id", "name", "host"):
            if not isinstance(inv.get(key), str) or not inv[key].strip():
                raise ValueError(f"{at}.{key} must be a non-empty text")
        if inv["id"] in ids:
            raise ValueError(f"{at}.id {inv['id']!r} is used twice")
        ids.add(inv["id"])
        if isinstance(inv["port"], bool) or not isinstance(inv["port"], int) or not 0 < inv["port"] < 65536:
            raise ValueError(f"{at}.port must be 1-65535")
        if isinstance(inv["device_id"], bool) or not isinstance(inv["device_id"], int) or not 0 <= inv["device_id"] <= 247:
            raise ValueError(f"{at}.device_id must be 0-247")
        checked.append(inv)
    settings["inverters"] = checked
    return settings


# Decoding -------------------------------------------------------------------

def decode(registers) -> dict:
    """Holding registers 178..190 (13 values) -> engineering values."""
    registers = list(registers)
    if len(registers) < REGISTER_COUNT:
        raise ValueError(f"expected {REGISTER_COUNT} registers, got {len(registers)}")

    def reg(number):
        value = registers[number - FIRST_REGISTER]
        if not isinstance(value, int) or not 0 <= value <= 0xFFFF:
            raise ValueError(f"register {number}: {value!r} is not a 16-bit value")
        return value

    battery = reg(190)
    if battery >= 0x8000:
        battery -= 0x10000
    pv1, pv2 = reg(186), reg(187)
    return {
        "battery_voltage_v": reg(183) / 100,
        "battery_soc_pct": reg(184),
        "pv1_power_w": pv1,
        "pv2_power_w": pv2,
        "pv_total_w": pv1 + pv2,
        "battery_power_w": battery,
        "load_power_w": reg(178),
    }


def battery_direction(power_w):
    if power_w is None:
        return None
    if abs(power_w) < IDLE_BATTERY_W:
        return "IDLE"
    return "DISCHARGING" if power_w > 0 else "CHARGING"


# Modbus ---------------------------------------------------------------------

def _read_holding(client, device_id):
    """read_holding_registers across pymodbus versions: device_id= (3.10+),
    slave= (3.x before 3.10), unit= (2.x)."""
    for keyword in ("device_id", "slave", "unit"):
        try:
            return client.read_holding_registers(FIRST_REGISTER, count=REGISTER_COUNT,
                                                 **{keyword: device_id})
        except TypeError as error:
            if keyword not in str(error):
                raise
    raise ReaderUnavailable("this pymodbus version accepts none of device_id/slave/unit")


def pymodbus_reader(host, port, device_id, timeout):
    """One read: connect, holding registers 178-190, close. Returns 13 ints."""
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError as error:
        raise ReaderUnavailable("pymodbus not installed in this Python") from error
    # pymodbus logs every failed connect; the error is reported in the view
    # instead, so an offline inverter does not fill the journal every 5 s.
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    client = ModbusTcpClient(host, port=port, timeout=timeout)
    try:
        if not client.connect():
            raise ConnectionError(f"no connection to {host}:{port}")
        result = _read_holding(client, device_id)
        if result.isError():
            raise IOError(f"Modbus error response: {result}")
        return list(result.registers)
    finally:
        client.close()


# Pollers --------------------------------------------------------------------

class InverterPoller:
    def __init__(self, inverter, settings, reader, clock, wall):
        self.inverter = inverter
        self.settings = settings
        self.reader = reader
        self.clock, self.wall = clock, wall
        self._lock = threading.Lock()
        self._reading = None
        self._last_ok = None            # monotonic
        # Monotonic time when the last good read was requested: the reading
        # shows the plant no earlier than this (energy.py probe freshness).
        self._read_at = None
        self._last_ok_utc = None
        self._last_attempt_utc = None
        self._last_duration_s = None
        self._error = None
        self._failures = 0              # consecutive
        self._attempts = 0
        self._unavailable = False

    def poll_once(self):
        inv = self.inverter
        started, started_wall = self.clock(), self.wall()
        try:
            reading = decode(self.reader(inv["host"], inv["port"], inv["device_id"],
                                         self.settings["timeout_s"]))
        except ReaderUnavailable as error:
            with self._lock:
                self._attempts += 1
                self._unavailable = True
                self._error = str(error)
                self._last_attempt_utc = started_wall
            return
        except Exception as error:      # refused, timeout, Modbus error, bad data
            with self._lock:
                self._attempts += 1
                self._failures += 1
                self._unavailable = False
                self._error = f"{type(error).__name__}: {error}"
                self._last_attempt_utc = started_wall
                self._last_duration_s = round(self.clock() - started, 3)
            return
        now = self.clock()
        with self._lock:
            self._attempts += 1
            self._failures = 0
            self._unavailable = False
            self._error = None
            self._reading = reading
            self._last_ok = now
            self._read_at = started
            self._last_ok_utc = started_wall
            self._last_attempt_utc = started_wall
            self._last_duration_s = round(now - started, 3)

    def view(self) -> dict:
        now = self.clock()
        stale_after = self.settings["stale_after_s"]
        with self._lock:
            age = None if self._last_ok is None else round(now - self._last_ok, 2)
            if self._unavailable:
                state = "UNAVAILABLE"
            elif self._attempts == 0:
                state = "WAITING"
            elif age is not None and age <= stale_after:
                state = "OK"
            elif self._failures:
                state = "OFFLINE"
            else:
                state = "STALE"
            inv = self.inverter
            reading = dict(self._reading) if self._reading else None
            if reading:
                reading["battery_direction"] = battery_direction(reading["battery_power_w"])
            return {
                "id": inv["id"], "name": inv["name"], "host": inv["host"],
                "port": inv["port"], "device_id": inv["device_id"],
                "state": state, "fresh": state == "OK",
                "age_s": age, "last_ok_utc": utc_text(self._last_ok_utc),
                "read_at": self._read_at,     # monotonic, controller clock
                "last_attempt_utc": utc_text(self._last_attempt_utc),
                "read_duration_s": self._last_duration_s,
                "error": self._error, "consecutive_failures": self._failures,
                "reading": reading,     # last good values, also when not fresh
            }


class SolarLink:
    def __init__(self, settings, *, reader=pymodbus_reader, clock=time.monotonic, wall=time.time):
        self.settings = settings
        self.clock = clock
        self.pollers = [InverterPoller(inv, settings, reader, clock, wall)
                        for inv in settings["inverters"]]
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        if not self.settings["enabled"]:
            return
        for poller in self.pollers:
            thread = threading.Thread(target=self._run, args=(poller,),
                                      name=f"solar-{poller.inverter['id']}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _run(self, poller):
        period = self.settings["poll_s"]
        while not self._stop.is_set():
            started = self.clock()
            poller.poll_once()
            self._stop.wait(max(0.1, period - (self.clock() - started)))

    def stop(self):
        self._stop.set()

    def poll_once(self):
        """All inverters once, one after the other (tests, tools)."""
        for poller in self.pollers:
            poller.poll_once()

    def view(self) -> dict:
        inverters = [p.view() for p in self.pollers]
        fresh = [i for i in inverters if i["fresh"]]
        missing = [i["name"] for i in inverters if not i["fresh"]]

        def total(key):
            return None if not fresh else sum(i["reading"][key] for i in fresh)

        socs = [i["reading"]["battery_soc_pct"] for i in fresh]
        battery = total("battery_power_w")
        s = self.settings
        return {
            "enabled": s["enabled"],
            "poll_s": s["poll_s"], "timeout_s": s["timeout_s"],
            "stale_after_s": s["stale_after_s"],
            "battery_charge_sign_verified": s["battery_charge_sign_verified"],
            "inverters": inverters,
            "totals": {
                "pv_w": total("pv_total_w"),
                "battery_w": battery,
                "battery_direction": battery_direction(battery),
                "load_w": total("load_power_w"),
                "complete": bool(fresh) and not missing,
                "included": [i["name"] for i in fresh],
                "missing": missing,
            },
            "soc_pct": {i["id"]: (i["reading"]["battery_soc_pct"] if i["reading"] else None)
                        for i in inverters},
            "soc_mismatch": len(socs) > 1 and max(socs) - min(socs) > s["soc_mismatch_pct"],
            "soc_mismatch_pct": s["soc_mismatch_pct"],
        }
