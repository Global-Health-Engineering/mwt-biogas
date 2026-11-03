# mwt-biogas

PLC software for the biogas fecal-sludge pasteurization plant in Mzuzu,
running on a Revolution Pi (Python 3.11.2).

Status: under development. The complete plant controller runs against a
plant simulator; there is no hardware output writer yet, so no program writes
hardware outputs automatically. `model_validated` is false, all control
settings are PROVISIONAL (`settings_validated: false`) and physical
assignments are unverified. The behaviour follows the control specification
"Pasteurizer Control Specification v0.1".

## Layers

| Layer | Modules | Rule |
|---|---|---|
| 1 Raw I/O | `hardware.py` (reader), future output writer | Only layer that knows RevPi channel names |
| 2 Signals | `signals.py`, `levels.py`, `flows.py`, `temperatures.py`, `modes.py`, `valves.py` | Units, validity, debounce, freshness; one `SignalSnapshot` per cycle |
| 3 Equipment | `transfer_pumps.py`, `transfer_flow.py`, `manhole_drain.py`, `overflow_valves.py`, `heating.py`, `treatment_control.py` | Return requests keyed by `OutputCommands` field names; never write hardware |
| 4 Process | `controller/` package, `process_state.py`, `arbiter.py`, `fault_manager.py` | One scan cycle: modes, states, faults, permissions; the arbiter turns requests + permissions into one complete `OutputCommands` frame |

Shared: `config.py` and `control_settings.py` (load and validate all
`config/*.json` once), `contracts.py` (typed data between layers), `clock.py`
(real and fake time), `alarms.py` / `alarm_manager.py`, `model_interface.py`
(model result contract), `operator_interface.py` (roles, manual requests,
level overrides), `persistence.py` (latched faults, STOP hold, retry
allowance), `event_log.py` (UTC + clock-sync + monotonic + boot ID +
sequence; queues records for the historian), `clock_sync.py` (kernel NTP
state), `historian/` (see below), `alarm_catalogue.py` (equipment and consequence per alarm, for the GUI), `publisher.py` (read-only status publisher on localhost, see "Read-only GUI"), `watchdog.py` (heartbeat; simulation stand-in), `shadow_outputs.py` (output writer for shadow runs: keeps commands, writes nothing), `simulated_io.py` (plant
simulator with PRETEND parameters).

`controller/` holds one responsibility per module:

| Module | Responsibility |
|---|---|
| `inputs.py` | One immutable `CycleInputs` per cycle (signals, mode, overrides, model usability) |
| `fault_evaluation.py` | Every fault condition, every cycle -> `FaultFlags` |
| `conditions.py` | Start, entry and permission rules as pure functions |
| `state_machine.py` | Process states and transitions |
| `treatment.py` | Release, stop criteria, low-flow retry, interruptions, Y4 controllers |
| `y4_monitor.py` | Y4 position, feedback and output supervision |
| `output_requests.py` | Requested frame and permissions per mode |
| `state_keeper.py` | Persistent state; save failures are critical and retried |
| `operator_commands.py` | GUI commands with role checks and logging |
| `recorder.py` | Event records (on change) and periodic process samples |
| `status.py` | Published status snapshot: identity, requested / commanded / written outputs, lamp modes, model summary, alarm details |
| `plant_controller.py` | Wiring and the scan cycle |

`startup.py` is superseded by the plant controller's preheat logic and kept
only for the signal pipeline demo.

## Conventions

- Modules do not call `time.monotonic()`; time is passed in, so simulations
  can use `FakeClock`.
- Freshness is decided once in `signals.py` (`config/acquisition_settings.json`);
  equipment uses `SignalSnapshot.available`.
- Unknown is never turned into a valid zero/false: invalid readings carry
  `valid=False` and an `error`.
- Above Layer 1, outputs are named by logical command (`transfer_pump_1`);
  `equipment_roles.json` → `command_outputs` maps them to equipment, and
  `io_mapping.json` maps equipment to channels.

## Configuration

`config/*.json` is validated at startup; a typo or out-of-range value stops
the program with the file and field named. `PlantConfig.config_hash`
identifies the exact configuration for logs.

## Historian

Events (state/mode transitions, alarms, operator actions, overrides,
model status, output requests vs written frames, clock corrections,
configuration changes, gaps) and periodic process samples are stored in a
local SQLite database, by default `logs/historian.sqlite`.

- The control scan only stamps records and appends them to a bounded queue
  (`logging.queue_capacity`); a writer thread stores them every
  `flush_interval_s`. Slow or missing storage never delays the scan.
- Queue full: new records are dropped and counted; samples may fill only
  half the queue, so events are kept first.
- Every record, and every CSV row, starts with `source` (LIVE plant I/O or
  SIMULATED, taken from the input reader, which must declare it) and
  `clock_source` (SYSTEM clock or SIMULATED time). Rows written before
  these columns existed show UNKNOWN.
- Storage failure or drops raise the self-clearing yellow LOGGER_FAILURE;
  after recovery a LOG_GAP event states the outage and dropped counts.
  This is separate from the critical controller-state file
  (`persistence.py`, STATE_SAVE_FAILED).
- Retention and size limits: `config/control_settings.json` → `logging`.
  Age limits apply only while the clock is not known to be unsynchronised.
  Each maintenance run (at start, then hourly) is logged as HISTORIAN_STORAGE.
- A corrupt database (quick check at start, or any write) is renamed to
  `historian.corrupt-<UTC>[-n].sqlite` (never overwriting an earlier one) and
  a new database is started; queued records go to the new file. Preserved
  files are never deleted automatically and are reported separately
  (`corrupt_files`, `corrupt_mb`; not counted against `max_database_mb`).
  A recovery raises the yellow HISTORIAN_DB_RECOVERED warning, which stays
  (also over a restart) until an operator acknowledges it; acknowledging
  does not require deleting the preserved files and never clears an active
  LOGGER_FAILURE. One acknowledgement covers every recovery still pending;
  its HISTORIAN_RECOVERY_ACKNOWLEDGED record lists each recovery event
  (time, run, sequence, error) and preserved file. A later recovery raises
  the warning again.
- `PlantController.shutdown()` first writes the plant safe command frame
  and logs it as OUTPUT_REQUESTED and OUTPUT_WRITTEN / OUTPUT_WRITE_FAILED
  (cause SHUTDOWN), then saves the controller state, then records
  CONTROLLER_STOP and flushes the historian within a timeout; flushing can
  never delay the safe frame.
- Databases and CSV exports are generated files and are not in Git.

| Module | Responsibility |
|---|---|
| `record_queue.py` | Bounded hand-over queue, drop counting |
| `writer.py` | Writer and its thread, retry, retention |
| `service.py` | `Historian` facade used by the event log |
| `sqlite_store.py` | SQLite tables `events`, `samples` (compressed), `boots` |
| `quarantine.py` | Detect and preserve corrupt databases; disk-usage report |
| `memory_store.py` | In-memory store for simulation and tests |
| `csv_export.py` | Read-only CSV export |
| `queries.py` | Read-only, bounded GUI queries: events (paged, filtered, live follow), trends (decimated, gaps, runs) |

## Read-only GUI

A browser view of the plant in a separate process (`gui/`). It shows the
EDiBl and pasteurizer drawings after GUI_V2, active alarms, trends and an
event log window. It sends no commands and never touches RevPi I/O.

    controller (scan)  --reference swap-->  StatusPublisher  127.0.0.1:8081  GET /status /config /health
    GUI process        --polls every 1 s-->  publisher; reads the historian SQLite file read-only
    browser            --polls every 1 s-->  GUI server     127.0.0.1:8080  /, /log, /api/...

- The publisher binds to localhost only and answers GET only (other methods:
  405). A failing or busy publisher can never stop or slow the scan.
- The data counts as live only while new snapshots arrive. After 2 s without
  a new snapshot the GUI shows STALE (publisher still answering) or
  DISCONNECTED, greys every value and marks it stale.
- Output layers are kept apart: requested (before the arbiter), commanded
  (arbitrated frame) and written (None when the write failed). Y4 shows
  requested, written and measured %. Pumps, heaters, Y1-Y3 and the burner
  have no feedback: the GUI shows "Command ON / OPEN", never a running or
  open state, and does not infer flame. The six manual valves on the drawing
  are "position not monitored".
- When the data is stale or disconnected, values are marked "(stale)", Y4
  and the alarm panel "last known".
- Latched alarms (level 2 and 3) show "Latched — reset required; cause not
  verified while stopped": the equipment is stopped, so the cause cannot be
  re-checked until reset.
- Tags and software ids: `gui/tags.py` (mapping confirmed 3 Nov 2026). The
  six P&ID manual valves EDB-VL-01, -02, -07, -08, -11, -12 are not drawn
  (simplified GUI_V2 layout kept).
- "Read-only (no login)" is not authentication. Both servers default to
  localhost (127.0.0.1). `--bind 0.0.0.0` makes the GUI listen on every
  network interface of the computer (LAN, Wi-Fi, VPN, Tailscale), not only
  Tailscale, with no login. Do not use it until access control is decided.

Run on a PC (Windows; use `python3` on Linux):

    python tools\run_simulation.py --speed 5                      # simulated plant + publisher
    python tools\run_simulation.py --speed 5 --fault transfer1_no_flow@60
    python tools\run_gui.py --database logs\simulation.sqlite     # second terminal
    # open http://127.0.0.1:8080/  (log window: "Open log in new window")

At speed > 1 the controller runs on a stepped simulated clock; all times are
marked SIMULATED.

| Module | Responsibility |
|---|---|
| `link.py` | Polls the publisher; WAITING / CONNECTED / STALE / DISCONNECTED |
| `server.py` | Pages, static files and read-only JSON / CSV API |
| `history_api.py` | Event descriptions, paging, trends, CSV export (max 100 000 rows) |
| `tags.py` | Drawing tags, names, kinds (what the GUI may claim), pipe legend |
| `static/` | Pages and scripts: drawings, alarms, trends, log window |

## Shadow run (RevPi: live inputs, outputs disabled)

The controller runs on the real inputs, in the mode set by the selector S1,
with its normal logic, supervision and interlocks. Every command goes to
`ShadowOutputWriter` (`pasteurizer/shadow_outputs.py`), which only keeps it in
memory: nothing is written to a RevPi output. The inputs are read by
`HardwareReader` with revpimodio2 in monitoring mode, which cannot write
outputs either.

    python3 tools/run_shadow.py                                  # terminal 1
    python3 tools/run_gui.py --database logs/shadow.sqlite       # terminal 2
    ssh -L 8080:127.0.0.1:8080 pi@<revpi-address>                # on the PC, then
                                                                 # open http://127.0.0.1:8080/

- The GUI shows "LIVE INPUTS — OUTPUTS DISABLED / SHADOW RUN" and hollow
  lamps (command only). Every output reads "not written — outputs disabled";
  requested and commanded values are shown separately. The log records
  OUTPUT_NOT_WRITTEN (reason OUTPUTS_DISABLED) instead of OUTPUT_WRITTEN, and
  the stop reports "safe frame NOT written".
- Nothing moves, so checks that wait for an actuator raise their faults
  (transfer no-flow, preheat timeout, Y4 position timeout). Flow calibration
  is pending: the GUI shows pulse counts and pulse rate, L/min is
  unavailable, and the flow checks block transfer and treatment in AUTO.
- `level_switch_10` (I_10_i06) is unused: disabled in `level_settings.json`,
  not read, no signal, no alarm, not in the GUI. An enabled level switch
  without a role in `equipment_roles.json` is refused at startup.
- Own files: `logs/shadow.sqlite` (history), `logs/shadow_state.json`
  (latched faults, STOP hold), `logs/shadow.lock` (held while running; a
  second shadow runner is refused). The runner refuses the simulation and
  live file names.

Reset (clears latched faults and STOP hold of the shadow run only):

1. Stop the runner with Ctrl+C and wait for "Shadow run stopped".
2. Check that it is gone: `pgrep -af run_shadow.py` prints nothing.
3. `rm logs/shadow_state.json`
4. Start the runner again.

Never delete the state file while the runner is running: it rewrites the
state on every change and at shutdown. Keep `logs/shadow.sqlite`.

Scripts in this repository that write outputs (none is used by the shadow
run; do not run them during it): `tools/output_mapping_test.py` (pulses
O_1..O_14 with `piTest -w`), `tools/led_test.py` (O_3, O_4, O_5),
`tools/valve_position_test.py` (writes the Y4 analogue command). Every other
script reads only (monitoring mode) or has no hardware access.

What is running on the RevPi (read-only checks, nothing is changed):

    ps -eo pid,user,etime,cmd | grep -Ei "python|revpi|node-?red|codesys|logi|pitest" | grep -v grep
    sudo fuser -v /dev/piControl0          # every process that has the process image open
    systemctl list-units --type=service --state=running --no-pager
    systemctl list-unit-files --state=enabled --no-pager
    systemctl status revpipyload --no-pager       # RevPiPyLoad (may not be installed)
    systemctl status nodered --no-pager           # Node-RED (may not be installed)
    crontab -l; sudo crontab -l; ls -l /etc/cron.d
    piTest -r O_1                                 # read one output's current value

## Desk checks (no hardware)

    python3 tools/control_scenarios.py      # 87 controller scenarios (tools/scenarios/)
    python3 tools/control_scenarios.py 74 75 76 77 78 79 80 81   # GUI, publisher, simulation runner, port binding
    python3 tools/control_scenarios.py 82 83 84 85 86 87         # shadow run, unused input, live timing
    python3 tools/control_scenarios.py 54 55 56 57 58 59 60 61 62 63 64 65 66 67 68   # historian only
    python3 tools/simulate_plant.py         # timeline of a simulated cold start
    python3 tools/simulate_plant.py --minutes 20 --historian logs/simulation.sqlite
    python3 tools/export_history.py samples exports/samples.csv --database logs/simulation.sqlite
    python3 tools/export_history.py events exports/events.csv --database logs/simulation.sqlite
    python3 tools/signal_pipeline_demo.py
    python3 tools/arbiter_demo.py
    python3 tools/alarm_demo.py
    PYTHONPATH=. python3 tools/output_demo.py

The other `tools/` scripts are read-only hardware monitors, or actuator test
utilities that write outputs (`output_mapping_test.py`, `led_test.py`,
`valve_position_test.py`); run those only under supervision.
