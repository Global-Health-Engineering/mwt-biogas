"""Start, entry and permission conditions (control spec sections 4, 6, 7, 8).

Pure functions: they read the cycle inputs and fault flags and decide, so
each rule of the specification can be read and tested on its own.
"""

from pasteurizer.modes import Mode


def heating_permitted(cs, inputs, *, heating_fault: bool, trip: bool) -> bool:
    """Spec 6: PT5/PT6 valid and below the trip limit, healthy inputs, no fault."""
    limit = cs.faults.overtemperature_trip_c
    return (inputs.acquisition_ok and inputs.selector_ok
            and inputs.pt5 is not None and inputs.pt6 is not None
            and inputs.pt5 < limit and inputs.pt6 < limit
            and not heating_fault and not trip)


def fallback_prerequisites(cs, inputs) -> bool:
    """Spec 8: fallback enabled, maximum flow configured, its sensors valid."""
    return (cs.fallback_enabled
            and cs.fallback.max_flow_l_min is not None
            and None not in (inputs.pt3, inputs.pt5, inputs.pt6, inputs.flow3))


def treatment_blocked(inputs, flags) -> bool:
    """Anything that forbids opening Y3/Y4 this cycle."""
    return (flags.treatment_fault or flags.summary.trip
            or not inputs.acquisition_ok or inputs.mode != Mode.AUTO)


def start_permitted(cfg, inputs, flags, *, stop_hold: bool) -> bool:
    """Spec 4: AUTO, no STOP hold, supply, healthy inputs and heating, Y4 closed."""
    return (inputs.mode == Mode.AUTO and not stop_hold
            and inputs.supply is True and inputs.acquisition_ok
            and flags.heating_ok
            and inputs.y4_closed(cfg.control.start.y4_closed_tolerance_percent))


def model_entry_ok(cs, inputs, flags) -> bool:
    """Spec 7: normal entry conditions for READY."""
    result = inputs.model
    return (inputs.model_ok and result is not None
            and inputs.pt5 is not None and inputs.pt5 >= cs.model.ready_pt5_min_c
            and result.achievable_flow_l_min >= cs.treatment_stop.min_flow_l_min
            and result.entry_target_met
            and inputs.supply is True
            and None not in (inputs.pt3, inputs.pt6, inputs.flow3)
            and not treatment_blocked(inputs, flags))


def fallback_entry_ok(cs, inputs, flags) -> bool:
    """Spec 8: fallback qualification conditions (dwell runs while true)."""
    return (flags.fallback_ready
            and inputs.pt5 >= cs.fallback.entry_pt5_min_c
            and inputs.supply is True
            and not treatment_blocked(inputs, flags))


def model_return_ok(inputs) -> bool:
    """Spec 8: return check from fallback to model treatment."""
    return (inputs.model_ok and inputs.model is not None
            and inputs.model.entry_target_met)
