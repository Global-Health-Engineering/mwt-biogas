"""3 kW heater drew no power: thermostat reached, or a heater fault?

The solar probe (energy.py) reports NO_LOAD (the load did not rise when the
3 kW was switched on) and LOAD_LOST (the load fell by about the heater's
power while it ran). Either means the heater draws no power, which is not a
solar verdict. This module decides what it means:

- PT5 valid and >= preheat.heater_3kw_thermostat_min_pt5_c (40 °C): the
  heater's own thermostat is taken as reached. Reported to the state machine,
  which ends the preheat (HEAT_UP: biogas circulation and 1 kW); event
  HEATER_3KW_THERMOSTAT with PT5, so the cut-out temperature can be read
  from the log.
- Otherwise (PT5 below the limit or invalid): Level 1 warning
  HEATER_3KW_NO_LOAD ("no electrical load registered when testing"). After
  preheat.heater_3kw_no_load_fault_count (3) such results in a row: Level 2
  HEATER_3KW_NO_LOAD_REPEATED ("check heater electronics"), 3 kW blocked
  until reset. The warning clears itself on the next result with load
  (probe passed or judged on solar), after the reset, and stays in the log.
"""

from dataclasses import dataclass

HEATER = "heater_3kw"
NO_POWER = ("NO_LOAD", "LOAD_LOST")
WITH_LOAD = ("PASS", "FAIL", "HOLD_FAIL")
WARNING = "HEATER_3KW_NO_LOAD"
BLOCKED = "HEATER_3KW_NO_LOAD_REPEATED"


@dataclass(frozen=True)
class ThermostatReached:
    pt5_c: float
    result: str          # NO_LOAD (at the probe) or LOAD_LOST (while running)
    reason: str


class Heater3kWLoadMonitor:
    def __init__(self, control_settings):
        preheat = control_settings.preheat
        self.min_pt5 = preheat.heater_3kw_thermostat_min_pt5_c
        self.limit = preheat.heater_3kw_no_load_fault_count
        self.count = 0          # no-power results in a row below the PT5 limit
        self.warning = False
        self.last_thermostat: ThermostatReached | None = None

    def update(self, energy_events, inputs, faults, log) -> ThermostatReached | None:
        """Once per cycle, after the energy evaluation."""
        reached = None
        raise_block = False
        for kind, data in energy_events:
            if kind != "ENERGY_PROBE" or data.get("heater") != HEATER:
                continue
            result = data.get("event")
            if result in WITH_LOAD:
                self.count = 0
                self.warning = False
            elif result in NO_POWER:
                pt5 = inputs.pt5
                if pt5 is not None and pt5 >= self.min_pt5:
                    reached = ThermostatReached(
                        pt5, result,
                        f"3 kW drew no power at PT5 {pt5:.1f} °C: heater thermostat reached")
                    self.last_thermostat = reached
                    log.record("HEATER_3KW_THERMOSTAT", pt5=pt5, result=result,
                               min_pt5=self.min_pt5, reason=reached.reason)
                else:
                    self.count += 1
                    self.warning = True
                    raise_block = self.count >= self.limit
                    log.record("HEATER_3KW_NO_LOAD", pt5=pt5, result=result,
                               count=self.count, limit=self.limit,
                               reason=(f"No electrical load registered when testing "
                                       f"({self.count}/{self.limit}); PT5 "
                                       f"{'invalid' if pt5 is None else f'{pt5:.1f} °C'} "
                                       f"below {self.min_pt5:g} °C: not the thermostat"))
        # The block latches; True only in the cycle it is raised, so a reset
        # is possible afterwards (cause absent).
        faults.set(BLOCKED, True if raise_block else False)
        if self.count >= self.limit and not raise_block and not faults.listed(BLOCKED):
            self.count = 0              # reset by the operator
            self.warning = False
        faults.set(WARNING, self.warning)
        return reached

    @staticmethod
    def blocked(faults) -> bool:
        return faults.listed(BLOCKED)
