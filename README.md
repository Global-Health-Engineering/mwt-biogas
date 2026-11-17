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
simulator with PRETEND parameters), `solar.py` (Deye inverters over Modbus TCP, read-only), `energy.py` (solar permission for the electric heaters), `simulated_solar.py` (simulated inverter pair).

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
- Signal events (`pasteurizer/controller/signal_watch.py`, logging only, no
  effect on control) show what the inputs did, on change only:
  LEVEL_CHANGED (debounced switch, actual reading; overrides noted),
  SENSOR_VALIDITY (any sensor invalid / valid again, in every plant state,
  after `logging.signal_validity_hold_s`; not per sensor during an
  acquisition loss), FLOW_STARTED / FLOW_STOPPED (flow above
  `flow_activity_min_l_min`, or `flow_activity_min_pulse_hz` without a
  K-factor, for `flow_activity_hold_s`; the stop records duration, litres,
  average and peak flow and the cause).
- OUTPUT_REQUESTED records each changed command's reason and blocking
  protections (`reasons`), the reason it had just before (`previous`, e.g.
  what kept a pump off), and the signal events of the same scan
  (`same_cycle_signals`). Every event written during a scan carries its
  cycle number (`data.cycle`); the log window highlights all events of a
  scan when one is clicked, and its "Operator" view hides signal events.

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
| `static/` | Drawings, alarms, trends, log window, manual panel (`manual.js`), Solar tab (`solar.js`) |

### Solar permission for the electric heaters (Deye inverters)

Two Deye 5 kW hybrid inverters run in parallel on one battery. Their WiFi
dataloggers answer Modbus TCP on port 502 (device ID 1). **The controller is
the only Modbus client** (`pasteurizer/solar.py`, one background thread per
inverter, every 5 s; the scan never waits for Modbus). The GUI shows the data
from the controller's status (Solar tab, `GET /api/solar`).

    controller --Modbus TCP, read-only--> inverters in solar_settings.json (port 502)
    controller: energy.py -> arbiter rule 4 -> heater_1kw / heater_3kw
    GUI: /api/solar <- controller status (inverters, permission, reasons)

Rules (`pasteurizer/energy.py`, settings in `arbiter_settings.json` `energy`).

**Single heater (1 kW or 3 kW): solar probe.** With a full battery the
inverters curtail PV to what is used, so the surplus with the heater off says
little. The heater is switched on as a test and the solar power that comes in
under load is measured:

| Step | Rule (setting, value) |
|---|---|
| Start | heat request (AUTO cold start or assist, MANUAL ON), both inverters current, SOC >= `soc_start_min_percent` (70 %), PV >= `probe_min_pv_w` (50 W; 0 W at night: no test), no interlock holding the heater off, no retry delay running |
| Switch on | until the last written frame has the heater on; deadline `probe_switch_on_timeout_s` (30 s, longer than the 10 s switching pause) |
| Settle | `probe_settle_s` (10 s) after it was written on |
| Measure | the next reading of **each** inverter requested after the settle point (`read_at`; a combined value can still hold an older reading of one inverter); deadline `probe_measure_timeout_s` (30 s) |
| Pass | solar contribution = (PV - other loads) / heater power >= `probe_min_solar_contribution` (0.70) and combined battery discharge <= `probe_max_battery_discharge_w` (853 W, background loads included) -> keeps heating |
| Fail / abort | heater off, next probe after `probe_retry_delay_s` (10 min), then only with a heat request and PV: probe failed, a deadline missed, inverter data stale during the probe |
| Running | contribution too low or discharge too high for `probe_hold_s` (120 s) -> off + retry delay; no inverter data for `communication_timeout_s` (60 s) -> off + retry delay |
| Any time | SOC <= `soc_stop_at_percent` (50 %) -> off at once, also during a probe (on the first reading that shows it; inverters are read every 5 s); heat request ended or an interlock -> probe ends without retry delay |
| Retry now | Solar tab button (COMMISSIONING login, AUTO and MANUAL): ends the retry delay of the heater waiting for it; the next probe still needs the start conditions above (request, current data, SOC >= 70 %, PV, no interlock). Logged as `OPERATOR_ACTION ENERGY_FORCE_RETRY` |

- **Other loads** = combined load - rated power of the heaters on in the last
  written frame (no double counting). Limit: a heater cut out by its own
  thermostat is still subtracted.
- **Discharge limit**: with consistent readings (PV + battery = load), >= 70 %
  solar contribution means at most 30 % of the heater power from the battery
  (300 W for the 1 kW, 900 W for the 3 kW). For the 3 kW the 853 W limit is
  therefore slightly tighter (it needs about 72 %); otherwise it bites when
  the battery also feeds what the load register does not show
  (self-consumption, unmetered loads).
- **Battery sign and summing** (register 190 signed 16 bit, + = discharging;
  sum of both inverters, only when both are current): the balance residual
  PV + battery - load is **diagnostic only**: shown on the Solar tab, in every
  `ENERGY_PROBE` event and in the periodic samples (`energy`), flagged beyond
  max(`battery_balance_tolerance_w`, `battery_balance_tolerance_fraction` x
  load) (300 W, 25 %). An inverted sign shows as about twice the load. The
  installed inverters must still be checked on site with real charging and
  discharging readings.
- **Status**: the status line and the Treatment card show what the electric
  heating waits for (`heating_wait`: probe phase and deadline, retry
  countdown, missing PV / SOC); blocked heaters carry the same text in their
  `ENERGY_SOLAR` note (with the retry time as `remaining_s`).
- **Event log**: `ENERGY_PROBE` (START, ON, PASS, FAIL, STALE,
  SWITCH_ON_TIMEOUT, MEASURE_TIMEOUT, HOLD_FAIL, COMM_TIMEOUT, SOC_CUTOFF,
  REQUEST_ENDED, INTERLOCK, HEATER_OFF) with PV, load, battery per inverter,
  SOC, contribution, residual and read times; `ENERGY_PERMISSION` on changes.

**Both heaters together (4 kW)**: never permitted; there is no combined
solar rule yet (the old surplus rule and its `start/stop_surplus_fraction`
and `start/stop_delay_s` settings were removed: with curtailed PV it could
not work). A probe on the combined load is planned with the 4 kW heating.
- **Arbiter rule 4** removes heater requests without permission (note
  `ENERGY_SOLAR` with the reason, `ENERGY_UNKNOWN` without an evaluation). It
  never creates a request; temperature, state-machine and safety interlocks
  are unchanged. When both heaters are requested, the larger permitted one
  stays (both together are never permitted). With the preheat exclusion
  (rule 3) they are never requested together anyway.
- **The biogas heating** (`heating_circulation`) is never gated.
- **Applies in AUTO and MANUAL.** `energy.solar_permission_enabled: false`
  switches the rule off (e.g. commissioning at night).
- **AUTO cold start**: WAITING_FOR_SOLAR is the 3 kW heat request; when the
  probe may start, the permission is granted before the heater is on and the
  state moves to PREHEAT_3KW; a failed probe returns to WAITING_FOR_SOLAR and
  the retry delay holds.
- **WAITING_FOR_SOLAR**: a cold start (AUTO, every start check passed, PT5
  below the cold-start limit) without solar permission for the 3 kW waits
  in this state instead of PREHEAT_3KW; nothing heats and the transition
  reason names the missing surplus or SOC. It moves to PREHEAT_3KW once the
  3 kW is permitted, and back when the permission ends during preheat. It
  leaves like any active state (STOP, selector, faults, supply) and goes to
  HEAT_UP if PT5 reaches the preheat limit anyway, but only on a PT5
  refreshed by a circulation pulse (see AUTO heating below). A warm start needs no
  solar. Green lamp: double blink (on 0.3 s, off 0.3 s, on 0.3 s, off 2 s;
  `arbiter_settings.json` `indicators.solar_wait_pulse_seconds` /
  `solar_wait_pause_seconds`; the pulse must stay longer than the 0.2 s scan).
- **Preheat timeout** (`control_settings.json` `preheat.max_duration_s`, now
  5 h) counts only the time the 3 kW was written on, summed over one cold
  start including its solar waits: waiting for the sun never trips the plant.
- Event log: `ENERGY_PERMISSION` on every change (mode, reason, surplus, SOC).
- Battery power: positive = discharging (validated with the 1 kW heater
  test), negative = charging (confirmed on site, Nov 2026;
  `battery_charge_sign_verified: true` in `solar_settings.json`; with
  `false` the GUI marks charging values "sign unverified").
- Without pymodbus (or with `solar_settings.json` `enabled: false`) the
  electric heaters get no permission; the controller runs normally.
- Simulation (`tools/run_simulation.py`, scenarios) uses
  `pasteurizer/simulated_solar.py` (sunny by default; its load includes the
  heaters that are on).

### AUTO heating: circulation pulses in preheat, biogas heat diagnostic

On this installation, starting the heating-water circulation pump
(`heating_circulation`) also calls for burner ignition. The RevPi has no
burner enable and no flame feedback; ignition retries are left to the
burner's own controls. All timing below follows the **written** pump output
(`OutputTimer` in `pasteurizer/heating.py`), never a running feedback.

**Cold start** (`control_settings.json` `preheat_circulation`): in
PREHEAT_3KW the 3 kW heater and the circulation run together. The circulation is no longer in
`arbiter_settings.json` `preheat_exclusion.excluded_outputs`; transfer pumps,
1 kW, manhole drain and compressor still are. Heater power permissions
(solar probe for the 3 kW) and the safety interlocks are unchanged.

| Rule | Setting (provisional) |
|---|---|
| Pulse on stratification: valid PT6 - PT5 >= trigger (PREHEAT_3KW) | `trigger_delta_c` 5 °C |
| Mixing pulse when the pump has not run since the last pulse or the start of this cold start (PREHEAT_3KW) | `max_idle_s` 600 s |
| Refresh pulse: valid PT5 at the preheat limit read with the pump off (PREHEAT_3KW and WAITING_FOR_SOLAR) | `startup_settings.json` `preheat_complete_at_celsius` 64 °C |
| Pulse length (written on) | `min_run_s` 20 s |
| Written off after every pulse before any start | `min_off_s` 20 s |
| Start to start (every written pump start = burner ignition call), all triggers, restarts included | `min_start_interval_s` 300 s |
| -> HEAT_UP from PREHEAT_3KW or WAITING_FOR_SOLAR at 64 °C only on a PT5 read while the pump has been written on for `min_run_s` | refreshed reading |

- `min_start_interval_s` 300 s caps pulse starts at 12 per hour whatever the
  trigger (before: up to 90 per hour with a held PT6 - PT5). **Placeholder:**
  no burner cycling limit is documented in the project files; set it from
  the burner manual's permitted starts per hour. A trigger that comes while
  the interval runs waits (status `next_start_allowed_in_s`).
- **3 kW pause for the PT5 check** (PREHEAT_3KW): a valid PT5 at 64 °C that
  is not a refreshed reading holds the 3 kW off (`heater_3kw_hold`) until a
  refresh pulse has run `min_run_s`, waiting for the start interval if
  needed. Mixed PT5 still >= 64 °C -> HEAT_UP; below -> the 3 kW resumes and
  the next stagnant 64 °C reading pauses it again. The pause is not applied
  during the 3 kW solar probe (switching off would fail it); once the probe
  passed, a paused heater keeps its solar permission. Transfer pumps stay
  held during the pause; the preheat timeout counts only written-on time.
  Without the pause, the 3 kW would heat on for up to the interval: bulk
  estimate dT = P t / (m c) = 3000 W x 300 s / (500 kg x 4186 J/(kg K))
  = 0.43 K (500 L tank; 1.1 K for 200 L), losses ignored. The stagnant PT5
  near the heater or in a hot layer can rise much more; no data yet.
- `max_idle_s` 600 s: PT5 refreshed and the water mixed at least every
  ~10 min without stratification.
- **Solar loss** (PREHEAT_3KW -> WAITING_FOR_SOLAR) does not cut a running
  pulse: it finishes its `min_run_s` (a cut pulse is a short burner cycle);
  the next start waits for the start interval. The energy manager already
  delays a returning 3 kW permission (10 min retry after a failed probe, SOC
  back to 70 % after a 50 % cutoff); the start interval bounds burner calls
  even when the permission flaps faster.
- WAITING_FOR_SOLAR circulates only to finish a pulse or for the PT5 refresh
  check (no electric heat). A plain PT5 at 64 °C with the pump off never
  completes a preheat, from either state.
- STOP, selector, trips (overtemperature, PT5/PT6 invalid, preheat timeout)
  end a pulse at once.
- Status: `heating.preheat_circulation` (requested, trigger DELTA /
  MAX_IDLE, pump on/off time, `pt5_refreshed`, reason); every request change
  is in `OUTPUT_REQUESTED` with the reason.

**Second stage (HEAT_UP, READY, FALLBACK_QUALIFY, treatment)**: continuous
circulation under the existing interlocks, 1 kW assist unchanged.
**Biogas heat diagnostic** (`control_settings.json` `biogas_heat`, diagnostic
only, never an interlock):

| Step | Setting (provisional) |
|---|---|
| Circulation written on for the startup delay before judging | `startup_delay_s` 120 s |
| Heat detected: valid PT6 - PT5 >= threshold held for the hold time | `delta_min_c` 2 °C, `hold_s` 60 s |
| Initial check: not detected within the window -> warning | `observation_s` 900 s |
| After detection: below threshold for the loss time -> warning | `loss_s` 300 s |

- Status `heating.biogas_heat.status`: `CHECKING` ("Checking"), `DETECTED`
  ("Biogas heat detected"), `NOT_DETECTED` ("Biogas heat not detected"),
  `null` outside the AUTO second stage. Event `BIOGAS_HEAT` on every change
  (PT5, PT6, delta); the periodic sample carries the status.
- Warning `BIOGAS_HEAT_NOT_DETECTED` (Level 1, yellow): raised on a missed
  window or a sustained loss, cleared when heat qualifies again or when AUTO
  leaves the second stage. Circulation stays requested; the 1 kW keeps its
  control and solar permission. READY (PT5 >= 75 °C for 60 s) and treatment
  with the 1 kW alone stay possible under the unchanged entry and
  continuation criteria.
- Invalid PT5/PT6 never qualify and never count as a loss. Circulation
  stopping resets the qualification (status Checking, the warning is kept).
- Defaults are placeholders: tune from known-good and gas-off PT5/PT6 traces
  at commissioning. `loss_s` must outlast normal burner thermostat
  off-cycles near the top temperature (not modelled in the simulator).
- Simulator: PT6 reads 3 K above PT5 while circulating with gas;
  `biogas_power_w = 0` is the gas-off case. Scenarios 127-144.

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
    python3 tools/control_scenarios.py 110 111 112 113                # inverter reading, Solar tab data
    python3 tools/control_scenarios.py 114 115 116 117 118 119 120 121 # solar probe (121: fake Modbus inverters)
    python3 tools/control_scenarios.py 124 125 126                    # probe: MANUAL, high discharge, stale data
    python3 tools/control_scenarios.py 145 146                        # Retry now (bridges the probe retry delay)
    python3 tools/control_scenarios.py 122 123                        # WAITING_FOR_SOLAR, green double blink
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
