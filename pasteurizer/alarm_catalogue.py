"""What each alarm concerns and what the controller does about it (GUI text).

Equipment is named by its software id (OutputCommands field or signal name);
the GUI translates ids into drawing tags. Consequences restate the agreed
fault actions of control spec section 12; nothing here changes behaviour.
Troubleshooting and cabinet procedures are deferred: `procedure` stays None
until documented, and the GUI then shows "No documented procedure".
"""

from dataclasses import dataclass

PASTEURIZER_TRIP = "Pasteurizer tripped: Y3/Y4 closed, heaters and circulation off"
PLANT_SAFE = "Plant safe command frame: all process outputs off or closed"


@dataclass(frozen=True)
class AlarmInfo:
    equipment: tuple[str, ...]
    consequence: str
    procedure: str | None = None


def _transfer(line: int) -> dict:
    pump, flow = f"transfer_pump_{line}", f"flow_sensor_{line}"
    return {
        f"TRANSFER_{line}_NO_FLOW": AlarmInfo(
            (pump, flow), f"Pump {line} stopped until reset; no automatic retry"),
        f"TRANSFER_{line}_LOW_FLOW": AlarmInfo((pump, flow), "Warning only; pump continues"),
        f"TRANSFER_{line}_FLOW_INVALID": AlarmInfo(
            (pump, flow), f"Pump {line} stopped; restarts when the measurement recovers"),
        f"TRANSFER_{line}_FLOW_INVALID_PERSISTENT": AlarmInfo(
            (pump, flow), f"Pump {line} stopped until reset"),
    }


CATALOGUE: dict[str, AlarmInfo] = {
    # Plant-wide
    "ACQUISITION_UNAVAILABLE": AlarmInfo((), "Equipment relying on the inputs inhibited"),
    "ACQUISITION_LOST": AlarmInfo((), PLANT_SAFE),
    "SELECTOR_INVALID": AlarmInfo(
        ("mode_selector",),
        "Treatment, heating and transfer pumps inhibited; drain and overflow continue"),
    "SELECTOR_INVALID_PERSISTENT": AlarmInfo(("mode_selector",), PLANT_SAFE),
    "WATCHDOG_RESTART": AlarmInfo((), "Plant held stopped until reset"),
    "FAULT_STORAGE": AlarmInfo((), PLANT_SAFE),
    "LOGGER_FAILURE": AlarmInfo((), "Records delayed or dropped; control continues"),
    "HISTORIAN_DB_RECOVERED": AlarmInfo(
        (), "New historian database started; control continues"),
    "STATE_SAVE_FAILED": AlarmInfo((), PLANT_SAFE + "; starts and resets refused"),
    "OUTPUT_WRITE_FAILED": AlarmInfo((), PLANT_SAFE + " (requested; not confirmed)"),
    # Pasteurizer
    "PT3_INVALID": AlarmInfo(("PT3_B4",), "Y3/Y4 closed; heating continues"),
    "PT3_INVALID_PERSISTENT": AlarmInfo(("PT3_B4",), PASTEURIZER_TRIP),
    "PT5_INVALID": AlarmInfo(("PT5_B6",), "Y3/Y4 closed; heaters and circulation off"),
    "PT5_INVALID_PERSISTENT": AlarmInfo(("PT5_B6",), PASTEURIZER_TRIP),
    "PT6_INVALID": AlarmInfo(("PT6_B7",), "Y3/Y4 closed; heaters and circulation off"),
    "PT6_INVALID_PERSISTENT": AlarmInfo(("PT6_B7",), PASTEURIZER_TRIP),
    "FLOW3_INVALID": AlarmInfo(("flow_sensor_3",), "Y3/Y4 closed; heating continues"),
    "FLOW3_INVALID_PERSISTENT": AlarmInfo(("flow_sensor_3",), PASTEURIZER_TRIP),
    "PT4_INVALID": AlarmInfo(("PT4_B5",), "Warning only; treatment continues"),
    "OVERTEMPERATURE": AlarmInfo(("PT5_B6", "PT6_B7"), PASTEURIZER_TRIP),
    "PREHEAT_TIMEOUT": AlarmInfo(("heater_3kw", "PT5_B6"), "3 kW off; " + PASTEURIZER_TRIP),
    "Y4_POSITION_TIMEOUT": AlarmInfo(("control_valve_percent",), PASTEURIZER_TRIP),
    "Y4_FEEDBACK_INVALID": AlarmInfo(
        ("control_valve_percent",), "Release inhibited: Y3/Y4 closed, no reopening"),
    "Y4_FEEDBACK_LOST": AlarmInfo(("control_valve_percent",), PASTEURIZER_TRIP),
    "LOW_TREATMENT_FLOW": AlarmInfo(
        ("flow_sensor_3", "control_valve_percent"),
        "Treatment stopped; one automatic retry after the wait"),
    "LOW_TREATMENT_FLOW_RETRY_FAILED": AlarmInfo(
        ("flow_sensor_3", "control_valve_percent"), PASTEURIZER_TRIP),
    "FALLBACK_FLOW_HIGH": AlarmInfo(
        ("flow_sensor_3", "control_valve_percent"), "Y3/Y4 closed; fallback requalifies"),
    "REPEATED_INTERRUPTIONS": AlarmInfo((), "Warning only; treatment continues"),
    "MODEL_UNRELIABLE_NO_FALLBACK": AlarmInfo((), PASTEURIZER_TRIP),
    # Model
    "MODEL_INPUT_INVALID": AlarmInfo(
        ("PT1_B2", "PT2_B3"), "Model unusable; fallback qualification instead"),
    "MODEL_UNRELIABLE": AlarmInfo((), "Fallback control continues"),
    # Overflow
    "OVERFLOW_1": AlarmInfo(("level_switch_6", "overflow_valve_1_open"), "Y1 opened"),
    "OVERFLOW_2": AlarmInfo(("level_switch_7", "overflow_valve_2_open"), "Y2 opened"),
    **_transfer(1),
    **_transfer(2),
}


def info(code: str) -> AlarmInfo:
    return CATALOGUE.get(code, AlarmInfo((), "Not documented"))
