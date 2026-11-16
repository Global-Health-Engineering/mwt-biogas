"""Simulated Deye inverter pair for the simulator and the scenarios.

view() returns the same shape as pasteurizer.solar.SolarLink.view(). Like the
real inverters, the measured load includes the electric heaters that the
simulated plant has on, so the energy manager's double-counting correction
is exercised.

    pv_w             PV the array could deliver now
    charge_accept_w  None: all of pv_w is produced (old behaviour). A number:
                     the battery takes at most this much charge, so produced
                     PV = min(pv_w, load + charge_accept_w) - curtailment with
                     a full battery: PV follows the load.
    soc_percent, base_load_w   as named; `available` False: no data at all;
    inverter_down    "inverter_2": that one OFFLINE (not fresh)
    battery_sign     -1 simulates an inverted battery sign (balance diagnostic)
    unmetered_w      battery power for loads or losses the load register does not
                     show (inverter self-consumption): discharge beyond the balance

With a clock, each inverter is polled every poll_s (inverter 2 half a period
later) and keeps that reading with its read_at time, like SolarLink: a
reading taken before a heater switched on does not contain it.
"""

from pasteurizer.solar import battery_direction

POWER_W = {"heater_1kw": 1000.0, "heater_3kw": 3000.0}
IDS = ("inverter_1", "inverter_2")


class SimulatedSolar:
    def __init__(self, plant=None, *, clock=None, pv_w=4000.0, base_load_w=300.0,
                 soc_percent=80.0, charge_accept_w=None, poll_s=5.0):
        self.plant = plant
        self.clock = clock
        self.pv_w = pv_w
        self.base_load_w = base_load_w
        self.soc_percent = soc_percent
        self.charge_accept_w = charge_accept_w
        self.poll_s = poll_s
        self.available = True
        self.inverter_down = None
        self.battery_sign = 1
        self.unmetered_w = 0.0
        self._snap = {}                 # inverter id -> (read_at, reading)
        self._next = {}                 # inverter id -> next poll time

    def heater_load_w(self):
        outputs = getattr(self.plant, "outputs", None)
        if outputs is None or not getattr(self.plant, "powered", True):
            return 0.0
        return sum(w for name, w in POWER_W.items() if getattr(outputs, name, False))

    def produced_pv_w(self, load):
        if self.charge_accept_w is None:
            return self.pv_w
        return max(0.0, min(self.pv_w, load + self.charge_accept_w))

    def _reading(self):
        load = self.base_load_w + self.heater_load_w()
        pv = self.produced_pv_w(load)
        battery = load - pv + self.unmetered_w  # + discharging, - charging
        half = 0.5
        return {
            "battery_voltage_v": 52.0, "battery_soc_pct": self.soc_percent,
            "pv1_power_w": round(pv * half / 2), "pv2_power_w": round(pv * half / 2),
            "pv_total_w": round(pv * half),
            "battery_power_w": round(self.battery_sign * battery * half),
            "load_power_w": round(load * half),
            "battery_direction": battery_direction(self.battery_sign * battery * half)}

    def _poll(self):
        """Take new readings where the poll period has passed (with a clock)."""
        if self.clock is None:
            reading = self._reading()
            for inverter_id in IDS:
                self._snap[inverter_id] = (None, reading)
            return
        now = self.clock()
        for n, inverter_id in enumerate(IDS):
            if inverter_id not in self._next:
                self._next[inverter_id] = now + n * self.poll_s / 2
            if now >= self._next[inverter_id] or inverter_id not in self._snap:
                self._snap[inverter_id] = (now, self._reading())
                while self._next[inverter_id] <= now:
                    self._next[inverter_id] += self.poll_s

    def view(self):
        if not self.available:
            return {"enabled": False, "reason": "Simulated: solar data unavailable"}
        self._poll()
        inverters = []
        for n, inverter_id in enumerate(IDS):
            down = self.inverter_down == inverter_id
            read_at, reading = self._snap[inverter_id]
            inverters.append({
                "id": inverter_id, "name": f"Inverter {n + 1}", "host": "simulated",
                "port": 502, "device_id": 1,
                "state": "OFFLINE" if down else "OK", "fresh": not down,
                "age_s": 99.0 if down else 1.0, "last_ok_utc": None, "last_attempt_utc": None,
                "read_at": read_at,
                "read_duration_s": 0.0, "error": "Simulated outage" if down else None,
                "consecutive_failures": 3 if down else 0, "reading": dict(reading)})
        fresh = [i for i in inverters if i["fresh"]]

        def total(key):
            return sum(i["reading"][key] for i in fresh) if fresh else None
        battery_total = total("battery_power_w")
        return {
            "enabled": True, "simulated": True,
            "poll_s": self.poll_s, "timeout_s": 3.0, "stale_after_s": 15.0,
            "battery_charge_sign_verified": False,
            "inverters": inverters,
            "totals": {"pv_w": total("pv_total_w"), "battery_w": battery_total,
                       "battery_direction": battery_direction(battery_total),
                       "load_w": total("load_power_w"),
                       "complete": len(fresh) == len(inverters),
                       "included": [i["name"] for i in fresh],
                       "missing": [i["name"] for i in inverters if not i["fresh"]]},
            "soc_pct": {i["id"]: self.soc_percent for i in inverters},
            "soc_mismatch": False, "soc_mismatch_pct": 2,
        }
