"""Simple plant simulator for desk testing the controller (no hardware).

SimulatedPlant is both the reader (read() returns snapshots in exactly the
HardwareReader format) and the writer (write() applies an OutputCommands
frame). Physics are deliberately crude and every number here is a PRETEND
value for software testing only: they are not plant parameters and prove
nothing about treatment.
"""

from dataclasses import dataclass, field
import math

from pasteurizer.clock import Clock
from pasteurizer.modes import Mode
from pasteurizer.output_commands import OutputCommands


@dataclass
class PlantParameters:
    """PRETEND values for simulation only."""

    bath_heat_capacity_j_k: float = 836_000.0     # ~200 L water
    ambient_c: float = 25.0
    bath_loss_w_k: float = 20.0
    biogas_power_w: float = 8000.0                # while circulation runs
    burner_thermostat_c: float = 86.0             # water heater's own limit
    effluent_cold_c: float = 25.0                 # PT1
    effluent_inlet_c: float = 45.0                # PT2 after heat exchanger
    coil_constant_l_min: float = 1.5              # PT3 = bath-(bath-PT2)*exp(-k/flow)
    y4_full_flow_l_min: float = 6.0               # gravity flow at Y4 100 %
    y4_speed_percent_s: float = 100.0 / 30.0
    transfer_pump_l_min: float = 5.0
    overflow_valve_l_s: float = 2.0
    pulses_per_litre: float = 2000.0
    tank_capacity_l: float = 1000.0
    line_low_l: float = 100.0
    line_high_l: float = 800.0
    line_overflow_l: float = 950.0
    effluent_low_l: float = 50.0
    effluent_high_l: float = 900.0
    drain_time_s: float = 30.0


@dataclass
class Faults:
    read_failure: bool = False
    write_failure: bool = False
    selector_both: bool = False
    invalid_temperatures: set = field(default_factory=set)
    temperature_offsets: dict = field(default_factory=dict)
    no_pulses: set = field(default_factory=set)       # flow sensor names
    counter_jump: set = field(default_factory=set)    # one backwards jump each
    counter_unstable: set = field(default_factory=set)  # discontinuity every step
    y4_stuck: bool = False
    y4_feedback_invalid: bool = False
    forced_switches: dict = field(default_factory=dict)


class SimulatedPlant:
    data_source = "SIMULATED"   # recorded with every historian record

    def __init__(self, config, clock: Clock, params: PlantParameters | None = None):
        self.cfg = config
        self.clock = clock
        self.p = params if params is not None else PlantParameters()
        self.faults = Faults()
        self.mode = Mode.AUTO
        self.sequence = 0
        self.outputs = OutputCommands()
        self.powered = True           # False after a watchdog shutdown

        self.tank = {1: 300.0, 2: 300.0}
        self.inflow_l_min = {1: 0.0, 2: 0.0}
        self.effluent_l = 500.0
        self.manhole_water = False
        self._drain_run_s = 0.0
        self.bath_c = 40.0
        self.y4_position = 0.0
        self.counters = {name: 0.0 for name in config.io_mapping["flow_counters"]}
        self.transfer_flow_scale = {1: 1.0, 2: 1.0}
        self._last = clock.monotonic()

    # ------------------------------------------------------------------
    # Physics
    # ------------------------------------------------------------------

    @property
    def treatment_flow_l_min(self):
        o = self.outputs
        if not o.pasteurizer_isolation_open or self.effluent_l <= 0:
            return 0.0
        return self.p.y4_full_flow_l_min * self.y4_position / 100.0

    def pt3_c(self):
        flow = self.treatment_flow_l_min
        if flow <= 0:
            return self.bath_c - 1.0
        k = self.p.coil_constant_l_min
        return self.bath_c - (self.bath_c - self.p.effluent_inlet_c) * math.exp(-k / flow)

    def step(self):
        now = self.clock.monotonic()
        dt = max(0.0, now - self._last)
        self._last = now
        if dt == 0:
            return
        p, o = self.p, self.outputs

        # Valve Y4 travel.
        if not self.faults.y4_stuck:
            target = o.control_valve_percent
            move = p.y4_speed_percent_s * dt
            self.y4_position += max(-move, min(move, target - self.y4_position))

        # Tanks.
        flows = {}
        for line in (1, 2):
            pump = getattr(o, f"transfer_pump_{line}") and self.tank[line] > 0
            flow = p.transfer_pump_l_min * self.transfer_flow_scale[line] if pump else 0.0
            flows[f"flow_sensor_{line}"] = flow
            self.tank[line] += (self.inflow_l_min[line] - flow) / 60.0 * dt
            if getattr(o, f"overflow_valve_{line}_open"):
                self.tank[line] -= p.overflow_valve_l_s * dt
            self.tank[line] = min(max(self.tank[line], 0.0), p.tank_capacity_l)
            self.effluent_l += flow / 60.0 * dt
        treatment = self.treatment_flow_l_min
        flows["flow_sensor_3"] = treatment
        self.effluent_l = min(max(self.effluent_l - treatment / 60.0 * dt, 0.0),
                              p.tank_capacity_l)

        # Manhole drain.
        if o.manhole_drain and self.manhole_water:
            self._drain_run_s += dt
            if self._drain_run_s >= p.drain_time_s:
                self.manhole_water = False
                self._drain_run_s = 0.0

        # Bath energy balance.
        burner = o.heating_circulation and self.bath_c < p.burner_thermostat_c
        power = (3000.0 * o.heater_3kw + 1000.0 * o.heater_1kw
                 + p.biogas_power_w * burner)
        power -= p.bath_loss_w_k * (self.bath_c - p.ambient_c)
        if treatment > 0:
            power -= treatment / 60.0 * 4180.0 * (self.pt3_c() - p.effluent_inlet_c)
        self.bath_c += power * dt / p.bath_heat_capacity_j_k

        # Flow counters.
        for name, flow in flows.items():
            if name in self.faults.no_pulses:
                continue
            self.counters[name] += flow / 60.0 * p.pulses_per_litre * dt
            if name in self.faults.counter_unstable:
                self.counters[name] = (self.counters[name] + 5e8) % 4e9
            if name in self.faults.counter_jump:
                self.counters[name] = max(0.0, self.counters[name] - 1e6)
                self.faults.counter_jump.discard(name)

    # ------------------------------------------------------------------
    # Reader
    # ------------------------------------------------------------------

    def _temperatures(self):
        p = self.p
        circulation = self.outputs.heating_circulation
        values = {
            "PT1_B2": p.effluent_cold_c,
            "PT2_B3": p.effluent_inlet_c,
            "PT3_B4": self.pt3_c(),
            "PT4_B5": 35.0,
            "PT5_B6": self.bath_c,
            "PT6_B7": self.bath_c + (3.0 if circulation else 0.0),
        }
        result = {}
        for name, value in values.items():
            value += self.faults.temperature_offsets.get(name, 0.0)
            invalid = name in self.faults.invalid_temperatures
            result[name] = {"raw": round(value * 10), "status": 2 if invalid else 0}
        return result

    def _levels(self):
        roles = self.cfg.level_roles
        p = self.p
        reached = {
            roles["manhole_water_present"]: self.manhole_water,
            roles["line_1_tank_low"]: self.tank[1] >= p.line_low_l,
            roles["line_2_tank_low"]: self.tank[2] >= p.line_low_l,
            roles["line_1_tank_high"]: self.tank[1] >= p.line_high_l,
            roles["line_2_tank_high"]: self.tank[2] >= p.line_high_l,
            roles["line_1_tank_overflow"]: self.tank[1] >= p.line_overflow_l,
            roles["line_2_tank_overflow"]: self.tank[2] >= p.line_overflow_l,
            roles["effluent_tank_low"]: self.effluent_l >= p.effluent_low_l,
            roles["effluent_tank_high"]: self.effluent_l >= p.effluent_high_l,
        }
        reached.update(self.faults.forced_switches)
        return reached

    def read(self):
        self.step()
        if self.faults.read_failure:
            raise RuntimeError("Simulated read failure")
        self.sequence += 1
        inputs = {name: False for name in self.cfg.io_mapping["digital_inputs"]}
        inputs.update(self._levels())
        inputs["manual_switch"] = self.mode == Mode.MANUAL or self.faults.selector_both
        inputs["auto_switch"] = self.mode == Mode.AUTO or self.faults.selector_both

        v = self.cfg.valve
        feedback = v.feedback_closed_raw + self.y4_position / 100.0 * (
            v.feedback_open_raw - v.feedback_closed_raw)
        command = v.command_closed_raw + self.outputs.control_valve_percent / 100.0 * (
            v.command_open_raw - v.command_closed_raw)
        return {
            "sampled_at": self.clock.monotonic(),
            "sequence": self.sequence,
            "module_status": {"dio": 0, "di": 0},
            "digital_inputs": inputs,
            "flow_counters": {name: int(value) & 0xFFFFFFFF
                              for name, value in self.counters.items()},
            "temperatures": self._temperatures(),
            "control_valve": {
                "command": round(command),
                "command_status": 0,
                "feedback": round(feedback),
                "feedback_status": 1 if self.faults.y4_feedback_invalid else 0,
            },
        }

    # ------------------------------------------------------------------
    # Writer
    # ------------------------------------------------------------------

    def write(self, commands: OutputCommands) -> bool:
        if not isinstance(commands, OutputCommands):
            raise TypeError("Expected OutputCommands")
        if self.faults.write_failure or not self.powered:
            return False
        self.outputs = commands
        return True

    def watchdog_shutdown(self):
        """What the independent watchdog does: process outputs off."""
        self.outputs = OutputCommands()
        self.powered = False
