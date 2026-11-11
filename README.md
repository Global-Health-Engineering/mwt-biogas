# mwt-biogas

PLC software for the biogas fecal-sludge pasteurization plant in Mzuzu,
running on a Revolution Pi (Python 3.11.2).

Status: commissioning. The plant controller runs on the RevPi with live
inputs and writes the outputs in every selector position (AUTO, OFF,
MANUAL), see "Controller on the RevPi". `model_validated` is false, all
control settings are PROVISIONAL (`settings_validated: false`), the flow
K-factors are unassigned and physical assignments are unverified. The
behaviour follows the control specification "Pasteurizer Control
Specification v0.1".

Open items

1. Software not yet implemented
   - Model integration: no live model worker feeds `submit_model_result`;
     `model_control_enabled` is false, so AUTO treats with the fallback
     controller only.
   - RevPi hardware watchdog (device-specific plan after `piTest -d`); it is
     the only cover for an OS hang.
   - The SIGINT/SIGTERM runner test (scenario 100) runs on Linux only.
2. On-site calibration and physical validation
   - Flow K-factors by the dedicated calibration procedure
     (`tools/flow_calibration.py`, see "Flow-sensor calibration"), then
     `calibration_confirmed: true` by hand; until then transfer pumps stay
     blocked.
   - PT100 temperature calibration.
   - Valve identification: O_11, O_13, O_14 drive Y3, Y1, Y2.
   - Y4 endpoints: 4-20 mA command against the measured open and closed
     positions.
   - Level-switch positions and drain timings.
   - Power loss: output states while the RevPi boots, power-loss positions of
     Y1-Y3, Y4 on loss of signal, restart into IDLE with persisted faults and
     no watchdog fault.
   - OS hang: hardware watchdog reset and timeout on the device, once the plan
     is agreed.
   - Service: systemd watchdog kill, ExecStopPost after a kill, restart limit,
     `User=pi` access to `/dev/piControl0`.
   - Clock synchronisation.

## Layers

| Layer | Modules | Rule |
|---|---|---|
| 1 Raw I/O | `hardware.py` (reader, monitoring mode), `hardware_outputs.py` (writer) | Only layer that knows RevPi channel names |
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
state), `historian/` (see below), `alarm_catalogue.py` (equipment and consequence per alarm, for the GUI), `publisher.py` (read-only status publisher on localhost, see "GUI"), `command_server.py` (GUI commands into the scan, localhost + token), `run_lock.py` (one controller on the hardware), `watchdog.py` (heartbeat; in-process software watchdog), `systemd_notify.py` (READY/WATCHDOG/STOPPING to systemd), `shadow_outputs.py` (output writer for shadow runs: keeps commands, writes nothing), `simulated_io.py` (plant
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

## GUI

A browser view of the plant in a separate process (`gui/`): EDiBl and
pasteurizer drawings after GUI_V2, alarms, trends, an event log window and a
Manual tab. Without a login it is read-only.

    controller (scan)  --reference swap-->  StatusPublisher  127.0.0.1:8081  GET /status /config /health
    controller (scan)  <--queue, between cycles--  CommandServer  127.0.0.1:8082  POST /command (token)
    GUI process        --polls every 1 s-->  publisher; reads the historian SQLite file read-only
    browser            --polls every 1 s-->  GUI server     127.0.0.1:8080  /, /log, /api/...
    browser            --POST /api/login, /api/command-->  GUI server --> CommandServer

- Logins: `tools/manage_users.py` writes `secrets/users.json` (PBKDF2
  hashes, owner-only, not in git). OPERATOR: START, STOP, RESET, acknowledge.
  COMMISSIONING: additionally manual equipment requests, level overrides and
  the Y3/Y4 valve test, only with S1 in MANUAL. Sessions: HttpOnly +
  SameSite=Strict cookie, 30 min idle, 8 h at most; five wrong passwords lock
  the name for 60 s. Without the users file nobody can log in.
- Commands: every POST needs the header `X-Requested-With: mwt-gui`. The GUI
  server passes user and role to the controller's command server, which
  accepts only localhost and the token from `logs/command.token` (written at
  controller start, owner-only, removed at stop). Commands run in the scan
  thread between cycles; the controller checks role, selector and interlocks
  again and logs every command (OPERATOR_ACTION) with user and outcome.
- The publisher binds to localhost only and answers GET only (other methods:
  405). A failing or busy publisher can never stop or slow the scan.
- The data counts as live only while new snapshots arrive. After 2 s without
  a new snapshot the GUI shows STALE (publisher still answering) or
  DISCONNECTED, greys every value and marks it stale.
- Output layers are kept apart: requested (before the arbiter), commanded
  (arbitrated frame) and written (what reached the outputs). Pumps, heaters,
  Y1-Y3 and the burner have no feedback: the GUI shows "Command ON / OPEN",
  never a running or open state. Hollow lamps: command only, not written.
- Latched alarms (level 2 and 3) show "Latched — reset required; cause not
  verified while stopped".
- Tags and software ids: `gui/tags.py` (mapping confirmed 3 Nov 2026).
- Both servers default to localhost. Use the SSH tunnel from your PC.
  `--bind 0.0.0.0` listens on every network interface (LAN, Wi-Fi, VPN,
  Tailscale) and has no HTTPS: logins would travel unencrypted.

Desk test with the simulator (Windows; `python3` on Linux):

    python tools\manage_users.py add yourname --role COMMISSIONING
    python tools\run_simulation.py --speed 1 --selector MANUAL     # simulated plant, commands
    python tools\run_gui.py --database logs\simulation.sqlite      # second terminal
    # open http://127.0.0.1:8080/, log in, Manual tab

| Module | Responsibility |
|---|---|
| `link.py` | Polls the publisher; WAITING / CONNECTED / STALE / DISCONNECTED |
| `server.py` | Pages, static files, read-only JSON / CSV API, login and command routes |
| `auth.py` | Users file, password hashes, sessions, lockout |
| `history_api.py` | Event descriptions, paging, trends, CSV export (max 100 000 rows) |
| `tags.py` | Drawing tags, names, kinds (what the GUI may claim), pipe legend |
| `static/` | Drawings, alarms, trends, log window, manual panel (`manual.js`) |

## Controller on the RevPi (production: outputs enabled)

Once, on the RevPi:

    python3 tools/manage_users.py add yourname --role COMMISSIONING
    sudo cp deploy/mwt-biogas-controller.service deploy/mwt-biogas-gui.service /etc/systemd/system/
    sudo systemctl daemon-reload

Run (enable the services for start at boot only when commissioning allows):

    sudo systemctl start mwt-biogas-controller mwt-biogas-gui
    systemctl status mwt-biogas-controller --no-pager
    journalctl -u mwt-biogas-controller -f          # the controller's console
    sudo systemctl stop mwt-biogas-controller       # orderly stop, then all outputs off
    ssh -L 8080:127.0.0.1:8080 pi@<revpi-address>   # on the PC, then open http://127.0.0.1:8080/

By hand instead of systemd (no ExecStopPost, no restart, no systemd watchdog):
`python3 tools/run_controller.py` and `python3 tools/run_gui.py --database
logs/controller.sqlite` in two terminals.

- Inputs are read with revpimodio2 in monitoring mode; outputs are written by
  `HardwareOutputWriter` (one complete frame per cycle: O_1..O_14 and the Y4
  analogue command, 4000-20000 raw for 0-100 %). It writes all outputs off
  at start and at every stop.
- Every selector position writes the arbitrated frame: AUTO runs the plant by
  the agreed logic, OFF stops the process (drain and overflow valves keep
  their level logic), MANUAL takes requests from the GUI.
- Manual tab (COMMISSIONING, S1 in MANUAL): ON/OFF or Open/Close per output,
  Y4 in %, manhole drain STOP (inhibit) / Automatic, level overrides, Y3/Y4
  valve test, "All manual requests OFF". The table shows the RevPi channel,
  your request, commanded, written and the interlock or alarm.
- Request lifecycle: a request expires 30 s after its last renewal (overrides
  10 s); the page renews while open. A renewal only extends an active
  request: after an expiry, a clear, leaving MANUAL or a controller restart it
  is refused and the button must be pressed again. Logout, session expiry
  (30 min idle, 8 h at most) and a GUI stop withdraw that user's requests in
  the controller (GUI server); closing the page does too (best effort).
  Leaving MANUAL clears every request. Requests live in controller memory
  only, so a restart clears them.
- Flow is used for control only when the pulse measurement is valid and
  fresh and its K-factor is valid and confirmed (`pulses_per_litre` set and
  `calibration_confirmed: true` in `config/flow_settings.json`). An
  unconfirmed factor gives an L/min value marked "(unconfirmed)" for display
  and logging only; control treats it as an invalid measurement.
- Interlocks that restrict manual operation (spec rev 74, all intended):
  transfer pumps stay off while the flow factor is unassigned or unconfirmed
  (TRANSFER_n_FLOW_INVALID, Level 2 after 10 s); the manhole drain runs only
  on water at LS1 (manual STOP inhibits it until a new water event, release,
  expiry or a mode change; test the pump with the LS1 override "Force
  reached"); heaters wait 10 s after start and after each other (switching
  pause, break-before-make); Y4 waits for Y3; AUTO starts only with
  buffer-tank supply.
- Valve outputs O_11 (Y3), O_13 (Y1), O_14 (Y2) are marked "identity
  pending" until checked; the Manual tab is the way to check them.
- `--outputs disabled` (diagnostic): the same logic, nothing is written
  (`logs/shadow.sqlite`, `logs/shadow_state.json`).

Start, stop and failures (launched by `deploy/mwt-biogas-controller.service`):

| Event | What happens | Tested |
|---|---|---|
| Start | lock, all outputs off, first scan, READY=1 | scenarios 93, 100 |
| `systemctl stop` (SIGINT) / SIGTERM | current scan ends; safe frame, state, CONTROLLER_STOP, historian; all off; exit 0 | 100 (on Linux) |
| Unhandled error | same shutdown; abnormal stop recorded; exit 1; systemd restarts | 101 |
| Scan hang (1 s without heartbeat) | watchdog thread latches all off, records the stop; runner ends (exit 3), or ends itself 10 s later if the scan stays stuck | 96, 103 |
| Whole process hung | no WATCHDOG=1: systemd kills it after 3 s (WatchdogSec) | heartbeats tested (103); the kill needs the RevPi |
| Killed (SIGKILL) | outputs keep their last value until ExecStopPost `safe_outputs.py` switches all off and records the stop | 102 (ExecStopPost simulated) |
| Restart after any abnormal end | WATCHDOG_RESTART latched (plant Level 3), everything off until a GUI reset | 102 |
| Repeated failures | at most 3 starts in 10 min, then systemd gives up (outputs off) | systemd, on the RevPi |
| Power loss | outputs lose their supply and drop out (relays off, valves to their power-loss positions, Y4 loses its 4-20 mA signal); after power returns the RevPi boots, the enabled service writes all off and starts in IDLE with persisted faults and STOP hold, no watchdog fault (spec 13) | no: output states during boot, valve and Y4 power-loss positions to verify on site |
| OS hang (kernel or piControl frozen) | systemd and this software stop too; outputs keep their last value. Only the RevPi hardware watchdog (reset, then power-on behaviour) covers it | no: device-specific plan after `piTest -d` |

`tools/safe_outputs.py` run by hand is recovery after the fact, not
automatic protection; it refuses while a controller holds the lock.

Files: `logs/controller.sqlite` (history), `logs/controller_state.json`
(latched faults, STOP hold, abnormal-stop mark), `logs/controller.lock`,
`logs/command.token`. Latched faults: RESET in the GUI. To clear the state
file instead: stop the controller, check `pgrep -af run_controller.py`
prints nothing, delete it, start again; never while it is running.

Programs that write outputs: `tools/run_controller.py` (default),
`tools/safe_outputs.py` (all off), and the supervised test utilities
`tools/output_mapping_test.py` (O_1..O_14), `tools/led_test.py` (O_3, O_4,
O_5), `tools/valve_position_test.py` (Y4) and `tools/flow_calibration.py`
(transfer pump of the sensor under test, sensors 1 and 2 only). Never run a
test utility while the controller runs (`flow_calibration.py` takes the
controller lock and refuses). Every other script reads only or has no
hardware access.

### Flow-sensor calibration

    python3 tools/flow_calibration.py                    # asks for the sensor
    python3 tools/flow_calibration.py --simulate         # rehearsal, no hardware

Settings: `config/flow_calibration_settings.json` (pump runtime, planned runs,
rundown and warning limits; not read by the controller).

- Sensors 1 and 2: per run, counting starts, then the transfer pump runs for
  `pump_runtime_s` (60 s, 5 runs) while the source low switch is reached. The
  liquid is caught in a bucket before the buffer tank. Counting continues
  through the rundown; press Enter once nothing more runs in.
- Sensor 3: Enter opens the counting window, the valve is opened and closed
  by hand, Enter closes the window. No output is written.
- Enter the measured volume [L] (the reference), the net mass [kg] (density
  check only) and a note. Each run can be accepted, repeated or excluded with
  a reason; nothing is deleted. K = sum(pulses) / sum(volume) over accepted
  runs, without offset.
- Export per session, never overwritten:
  `exports/flow_calibration/<sensor>_calibration_<UTC>Z.csv` plus `.svg`
  plot; journal in `logs/flow_calibration/`. Both folders are git-ignored:
  copy the exports off the RevPi.
- Saving writes `pulses_per_litre` only after typing the sensor name, sets
  `calibration_confirmed` to false, backs up `config/flow_settings.json`
  (`.bak`) and appends `config/flow_calibration_history.json`.

What is running on the RevPi (read-only checks):

    ps -eo pid,user,etime,cmd | grep -Ei "python|revpi|node-?red|codesys|logi|pitest" | grep -v grep
    sudo fuser -v /dev/piControl0          # every process that has the process image open
    systemctl list-units --type=service --state=running --no-pager
    systemctl list-unit-files --state=enabled --no-pager
    crontab -l; sudo crontab -l; ls -l /etc/cron.d
    piTest -r O_1                          # read one output's current value

## Desk checks (no hardware)

    python3 tools/control_scenarios.py      # 105 controller scenarios (tools/scenarios/)
    python3 tools/control_scenarios.py 74 75 76 77 78 79 80 81   # GUI, publisher, simulation runner, port binding
    python3 tools/control_scenarios.py 82 83 84 85 86 87         # outputs disabled, unused input, live timing
    python3 tools/control_scenarios.py 88 89 90 91 92 93 94 95 96   # outputs, AUTO, commands, logins, watchdog
    python3 tools/control_scenarios.py 97 98 99 100 101 102 103 104 105 # requests, drain, stops, crashes, flow guard
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
`valve_position_test.py`, `flow_calibration.py`); run those only under
supervision and never while the controller runs.
