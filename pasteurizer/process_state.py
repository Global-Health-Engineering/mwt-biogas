"""Process states coordinated by the plant state machine (spec section 4)."""

from enum import Enum


class ProcessState(str, Enum):
    IDLE = "IDLE"                              # treatment isolated, process heat off
    # Cold start with every start check passed, but the solar permission for
    # the 3 kW heater is missing: no electric heat; preheat starts once
    # permitted. The circulation runs only to finish a pulse or to refresh a
    # PT5 reading at the preheat limit (each start calls the burner).
    WAITING_FOR_SOLAR = "WAITING_FOR_SOLAR"
    PREHEAT_3KW = "PREHEAT_3KW"                # cold-start heating, 3 kW only
    HEAT_UP = "HEAT_UP"                        # second-stage heating, effluent isolated
    READY = "READY"                            # model-path entry dwell running
    MODEL_TREATMENT = "MODEL_TREATMENT"        # model sets flow, flow PI drives Y4
    FALLBACK_QUALIFY = "FALLBACK_QUALIFY"      # fallback heating/dwell, isolated
    FALLBACK_TREATMENT = "FALLBACK_TREATMENT"  # PT3 step controller drives Y4
    STOPPING = "STOPPING"                      # intentional shutdown, ends in IDLE
    TRIPPED = "TRIPPED"                        # Level 3; isolated until reset


class TreatmentPath(str, Enum):
    MODEL = "MODEL"
    FALLBACK = "FALLBACK"


# Green blinks in these states (spec section 11).
HEAT_UP_STATES = frozenset({
    ProcessState.PREHEAT_3KW,
    ProcessState.HEAT_UP,
    ProcessState.READY,
    ProcessState.FALLBACK_QUALIFY,
})

TREATMENT_STATES = frozenset({
    ProcessState.MODEL_TREATMENT,
    ProcessState.FALLBACK_TREATMENT,
})

# States in which second-stage heating (circulation + 1 kW) may run.
SECOND_STAGE_STATES = frozenset({
    ProcessState.HEAT_UP,
    ProcessState.READY,
    ProcessState.MODEL_TREATMENT,
    ProcessState.FALLBACK_QUALIFY,
    ProcessState.FALLBACK_TREATMENT,
})

# One cold start: waiting for solar and preheating share timers and pulses.
PREHEAT_STATES = frozenset({ProcessState.WAITING_FOR_SOLAR, ProcessState.PREHEAT_3KW})

ACTIVE_STATES = PREHEAT_STATES | SECOND_STAGE_STATES
