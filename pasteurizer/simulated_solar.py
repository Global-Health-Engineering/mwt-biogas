"""Simulated Deye inverter pair for the simulator and the scenarios.

view() returns the same shape as pasteurizer.solar.SolarLink.view(). Like the
real inverters, the measured load includes the electric heaters that the
simulated plant has on, so the energy manager's double-counting correction
is exercised. Change pv_w, base_load_w, soc_percent or `available` at any
time; `inverter_down` takes one inverter offline.
"""

from pasteurizer.solar import battery_direction

POWER_W = {"heater_1kw": 1000.0, "heater_3kw": 3000.0}


class SimulatedSolar:
    def __init__(self, plant=None, *, pv_w=4000.0, base_load_w=300.0, soc_percent=80.0):
        self.plant = plant
        self.pv_w = pv_w
        self.base_load_w = base_load_w
        self.soc_percent = soc_percent
        self.available = True           # False: no data at all (monitoring down)
        self.inverter_down = None       # "inverter_2": that one OFFLINE

    def heater_load_w(self):
        outputs = getattr(self.plant, "outputs", None)
        if outputs is None or not getattr(self.plant, "powered", True):
            return 0.0
        return sum(w for name, w in POWER_W.items() if getattr(outputs, name, False))

    def view(self):
        if not self.available:
            return {"enabled": False, "reason": "Simulated: solar data unavailable"}
        load = self.base_load_w + self.heater_load_w()
        battery = round(load - self.pv_w)       # + discharging, - charging
        inverters = []
        for n, inverter_id in enumerate(("inverter_1", "inverter_2")):
            down = self.inverter_down == inverter_id
            share = 0.5
            reading = {
                "battery_voltage_v": 52.0, "battery_soc_pct": self.soc_percent,
                "pv1_power_w": round(self.pv_w * share / 2), "pv2_power_w": round(self.pv_w * share / 2),
                "pv_total_w": round(self.pv_w * share), "battery_power_w": round(battery * share),
                "load_power_w": round(load * share),
                "battery_direction": battery_direction(battery * share)}
            inverters.append({
                "id": inverter_id, "name": f"Inverter {n + 1}", "host": "simulated",
                "port": 502, "device_id": 1,
                "state": "OFFLINE" if down else "OK", "fresh": not down,
                "age_s": 99.0 if down else 1.0, "last_ok_utc": None, "last_attempt_utc": None,
                "read_duration_s": 0.0, "error": "Simulated outage" if down else None,
                "consecutive_failures": 3 if down else 0, "reading": reading})
        fresh = [i for i in inverters if i["fresh"]]

        def total(key):
            return sum(i["reading"][key] for i in fresh) if fresh else None
        battery_total = total("battery_power_w")
        return {
            "enabled": True, "simulated": True,
            "poll_s": 5.0, "timeout_s": 3.0, "stale_after_s": 15.0,
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
