"""Status snapshot published once per cycle for the GUI and logging.

Everything here is read from the cycle's own objects; building it never
touches storage or I/O. Field groups for the GUI:

  identity    data_source, clock_source, clock_sync, boot_id, utc, cycle, now
  outputs     requested (before the arbiter), commanded (the arbitrated
              frame), written (None when the write was not confirmed),
              arbitration_notes, lamp_modes (off / steady / blinking)
  treatment   state, path, state_reason, timers, retry, model summary
  alarms      AlarmView with raised time (UTC), equipment and consequence
  signals     actual and overridden readings; ages from `now` and each
              signal's monotonic sample time
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from pasteurizer.alarm_catalogue import info as alarm_info


@dataclass(frozen=True)
class AlarmView:
    code: str
    level: int
    scope: str
    active: bool
    acknowledged: bool
    message: str
    recovery: str
    severity: str = "WARNING"           # CRITICAL (Level 3) or WARNING
    raised_utc: str | None = None
    occurrences: int = 1
    equipment: tuple = ()               # software ids; the GUI maps them to tags
    consequence: str = ""
    procedure: str | None = None        # None: no documented procedure yet


@dataclass(frozen=True)
class ControllerStatus:
    cycle: int
    now: float
    mode: str
    state: str
    path: str | None
    state_reason: str
    stop_hold: bool
    release_permitted: bool
    requested: dict
    written: dict | None          # None: this cycle's frame was NOT physically written
    write_ok: bool                # True only for a confirmed physical write
    state_saved: bool             # persistent state confirmed stored
    arbitration_notes: list
    alarms: list
    lights: dict
    manual_requests: dict
    valve_test_override: bool
    overrides: list
    override_actual: dict         # physical readings beside overridden switches
    dwell_remaining_s: float | None
    supply_wait_remaining_s: float | None
    retry_available: bool
    model_usable: bool
    model_reason: str
    historian: dict = field(default_factory=dict)   # health, backlog, disk usage
    signals: dict = field(repr=False, default_factory=dict)
    # Identity of this snapshot (for the GUI header and staleness checks).
    data_source: str = "UNKNOWN"        # LIVE or SIMULATED
    clock_source: str = "UNKNOWN"       # SYSTEM or SIMULATED
    clock_sync: str = "UNKNOWN"         # SYNCHRONISED / UNSYNCHRONISED / UNKNOWN
    boot_id: str = ""
    utc: str = ""
    config_hash: str = ""
    commanded: dict = field(default_factory=dict)   # arbitrated frame, written or not
    lamp_modes: dict = field(default_factory=dict)  # green/yellow/red: off/steady/blinking
    model: dict = field(default_factory=dict)       # summary for the model block
    # False in a shadow run (live inputs, outputs disabled): nothing is ever
    # written, `written` stays None and output_write says why.
    outputs_enabled: bool = True
    output_write: str = ""      # WRITTEN / WRITE_FAILED / NOT_WRITTEN_OUTPUTS_DISABLED


def _iso(epoch) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def alarm_views(faults, *, now=None, wall=None) -> list:
    views = []
    for s in faults.current():
        details = alarm_info(s.code)
        raised = None
        if now is not None and wall is not None and s.raised_at is not None:
            raised = _iso(wall - (now - s.raised_at))
        views.append(AlarmView(
            s.code, s.definition.fault_level, s.scope.value, s.active, s.acknowledged,
            s.definition.message, s.definition.recovery,
            severity="CRITICAL" if s.definition.fault_level >= 3 else "WARNING",
            raised_utc=raised, occurrences=s.occurrences,
            equipment=details.equipment, consequence=details.consequence,
            procedure=details.procedure))
    return views


def model_summary(inputs, enabled: bool) -> dict:
    """What the model block shows; never a claim the model did not make."""
    result = inputs.model
    summary = {"enabled": enabled, "usable": inputs.model_ok, "reason": inputs.model_reason,
               "status": None, "sequence": None, "age_s": None, "predicted_pt3_c": None,
               "measured_pt3_c": inputs.pt3, "predicted_log_reduction": None,
               "achievable_flow_l_min": None, "parameter_version": None}
    if result is not None:
        summary.update(status=result.status.value, sequence=result.sequence,
                       age_s=round(inputs.now - result.input_end, 3),
                       predicted_pt3_c=result.predicted_pt3_c,
                       predicted_log_reduction=result.predicted_log_reduction,
                       achievable_flow_l_min=result.achievable_flow_l_min,
                       parameter_version=result.parameter_version)
    return summary


def build_status(*, cycle, inputs, process, requests, result, write_ok,
                 keeper, faults, manual, historian=None, identity=None,
                 lamp_modes=None, model_enabled=False,
                 outputs_enabled=True) -> ControllerStatus:
    """write_ok: the output layer accepted the frame. It counts as written only
    when outputs are enabled; a shadow writer's acceptance is never a write."""
    frame = result.commands
    physical = bool(write_ok and outputs_enabled)
    output_write = ("WRITTEN" if physical
                    else "NOT_WRITTEN_OUTPUTS_DISABLED" if write_ok else "WRITE_FAILED")
    now = inputs.now
    identity = identity or {}
    wall = identity.get("wall")
    return ControllerStatus(
        cycle=cycle,
        now=now,
        mode=getattr(inputs.mode, "value", "UNKNOWN"),
        state=process.state.value,
        path=getattr(process.path, "value", None),
        state_reason=process.reason,
        stop_hold=process.stop_hold,
        release_permitted=requests.release_permitted,
        requested=asdict(requests.commands),
        written=asdict(frame) if physical else None,
        write_ok=physical,
        state_saved=keeper.saved,
        arbitration_notes=[asdict(note) for note in result.notes],
        alarms=alarm_views(faults, now=now, wall=wall),
        lights={"green": frame.light_green, "yellow": frame.light_yellow,
                "red": frame.light_red},
        manual_requests=manual.active_requests(),
        valve_test_override=manual.valve_test_active,
        overrides=sorted(manual.overrides),
        override_actual=inputs.override_actual,
        dwell_remaining_s=process.dwell_remaining(now),
        supply_wait_remaining_s=process.supply_wait_remaining(now),
        retry_available=not process.treatment.retry_exhausted,
        model_usable=inputs.model_ok,
        model_reason=inputs.model_reason,
        historian=historian or {},
        signals=inputs.effective.to_dict(),
        data_source=identity.get("data_source", "UNKNOWN"),
        clock_source=identity.get("clock_source", "UNKNOWN"),
        clock_sync=identity.get("clock_sync", "UNKNOWN"),
        boot_id=identity.get("boot_id", ""),
        utc=_iso(wall) if wall is not None else "",
        config_hash=identity.get("config_hash", ""),
        commanded=asdict(frame),
        lamp_modes=lamp_modes or {},
        model=model_summary(inputs, model_enabled),
        outputs_enabled=outputs_enabled,
        output_write=output_write,
    )
