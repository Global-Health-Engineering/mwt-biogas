/* Main window: header, process views, context card, detail panel, alarms. */
"use strict";

const APP = { meta: null, view: null, tab: "edibl", selected: null };
const FLOW_STALE_S = 3.0;   // flow sample older than the flow sample-gap limit

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function isLive() { return APP.view && APP.view.state === "CONNECTED"; }

// Shadow run: live inputs, commands computed but never written to an output.
const SHADOW_TEXT = "LIVE INPUTS — OUTPUTS DISABLED / SHADOW RUN";
const NOT_WRITTEN = "not written — outputs disabled";
function isShadow(st) { return !!st && st.outputs_enabled === false; }
function writtenText(st, format) {
  if (isShadow(st)) return NOT_WRITTEN;
  return st.write_ok ? format(st.written) : "not confirmed (write failed)";
}
// Uncalibrated flow: the pulse input can be valid while L/min is unavailable,
// or shown from an unconfirmed factor; the controller uses neither for control.
const UNCONFIRMED = "calibration not confirmed: not used for control";
function flowText(f) {
  if (!f || !f.valid) return { value: "Unavailable", pulses: null, lpm: false };
  const hz = f.pulse_rate_hz == null ? "—" : f.pulse_rate_hz.toFixed(2);
  const pulses = `${f.raw_counter} pulses · ${hz} pulses/s`;
  if (f.flow_lpm == null) return { value: "L/min unavailable", pulses, hz, lpm: false };
  if (f.calibrated !== true) return { value: `${f.flow_lpm.toFixed(2)} L/min (unconfirmed)`, pulses, hz, lpm: false, unconfirmed: true };
  return { value: `${f.flow_lpm.toFixed(2)} L/min`, pulses, hz, lpm: true };
}

function word(id, on) {
  if (on === undefined || on === null) return "—";
  if (id.endsWith("_open")) return on ? "OPEN" : "CLOSED";
  if (id === "cabinet_fan_disabled") return on ? "DISABLED" : "THERMOSTAT";
  return on ? "ON" : "OFF";
}

/* Header ----------------------------------------------------------------- */
function renderHeader() {
  const v = APP.view, st = v && v.status;
  const src = st ? st.data_source : "UNKNOWN";
  const badge = $("#source");
  const shadow = isShadow(st);
  const live = !shadow && src === "LIVE";
  badge.textContent = shadow ? SHADOW_TEXT : live ? "LIVE — OUTPUTS ENABLED"
    : src === "SIMULATED" ? "SIMULATION" : "SOURCE UNKNOWN";
  badge.className = "badge " + (shadow ? "badge-shadow" : live ? "badge-outputs" : src === "SIMULATED" ? "badge-sim" : "badge-unknown");
  const banner = $("#shadow-banner");
  banner.hidden = !shadow;
  if (shadow) {
    banner.className = "shadow-banner";
    banner.innerHTML = `<b>${SHADOW_TEXT}.</b> Commands are computed from the real inputs but never written to an output: pumps, heaters and valves do not move. Hollow lamps show commands only. Faults that wait for an actuator response are expected.`;
  }

  const conn = $("#conn");
  const age = v && v.age_s != null ? `${v.age_s.toFixed(1)} s` : "—";
  const texts = {
    CONNECTED: `Connected · data ${age} old`,
    STALE: `STALE · no new controller snapshot for ${age} (server responding)`,
    DISCONNECTED: `DISCONNECTED · last data ${age} ago`,
    WAITING: "Waiting for the controller…",
  };
  conn.textContent = texts[v ? v.state : "WAITING"];
  conn.className = "conn conn-" + (v ? v.state.toLowerCase() : "waiting");
  document.body.classList.toggle("stale", !isLive());

  const mode = st ? (st.signals && st.signals.mode && st.signals.mode.valid === false ? "INVALID" : st.mode) : "—";
  const last = isLive() ? "" : " (last snapshot)";
  $("#mode").textContent = `Mode ${mode === "UNKNOWN" ? "INVALID" : mode}${last}`;
  $("#state").innerHTML = st
    ? `<b>${UI.esc(st.state)}</b>${st.path ? ` · ${UI.esc(st.path)} path` : ""}<span class="reason">${UI.esc(st.state_reason)}${last}</span>`
    : "—";

  const modes = st ? st.lamp_modes : {};
  for (const [colour, text] of [["green", "Pasteurizing"], ["yellow", "Warning"], ["red", "Fault"]]) {
    const el = $(`#lamp-${colour}`);
    const mode = isLive() && modes ? modes[colour] : "unknown";
    el.className = `plant-lamp lamp-${colour} mode-${mode}${isShadow(st) ? " cmd-only" : ""}`;
    let label = text + (mode === "blinking" ? " (blinking)" : mode === "off" ? " off" : "");
    if (colour === "red" && mode === "blinking") label = "Fallback path (red blinking)";
    if (mode === "unknown") label = `${text}: unknown`;
    if (isShadow(st) && mode !== "unknown") label += " — command, not written";
    el.title = label;
    $(`#lamp-${colour}-text`).textContent = label;
  }
  const fan = st && st.commanded ? st.commanded.cabinet_fan_disabled : null;
  $("#fan").textContent = fan === null || !isLive() ? "Cabinet fan: unknown"
    : fan ? "Cabinet fan: command DISABLED" : "Cabinet fan: command thermostat control";
  $("#clock").textContent = st && st.utc
    ? `Controller time ${UI.time(st.utc)} · ${UI.clockQuality(st.clock_source, st.clock_sync)}${isLive() ? "" : " (last snapshot)"}`
    : `Browser time ${UI.time(new Date().toISOString())} · no controller snapshot`;
}

/* Process overlays --------------------------------------------------------- */
function setText(sel, text, cls) {
  for (const el of $$(sel)) {
    el.textContent = text;
    if (cls !== undefined) el.setAttribute("class", el.getAttribute("class").split(" ")[0] + " " + cls);
  }
}

function renderEquipment() {
  const st = APP.view && APP.view.status;
  const live = isLive();
  if (!st || !APP.meta) return;
  const sig = st.signals || {};
  for (const e of APP.meta.equipment) {
    const id = e.id;
    if (e.kind === "command") {
      const cmd = st.commanded ? st.commanded[id] : undefined;
      const req = st.requested ? st.requested[id] : undefined;
      // Shadow run: hollow lamp = command only, nothing was written.
      const shown = !live || cmd === undefined ? "unknown" : isShadow(st) ? (cmd ? "cmd-on" : "cmd-off") : cmd ? "on" : "off";
      for (const lamp of $$(`[data-lamp="${id}"]`)) lamp.setAttribute("class", "lamp " + shown);
      // No physical feedback: the GUI states the command, never the actual state.
      let text = live ? `Command ${word(id, cmd)}` : "Unknown (stale)";
      if (live && !isShadow(st) && !st.write_ok) text += " · not written";
      setText(`[data-state="${id}"]`, text);
      // Requested differs from the command: an interlock or permission overrides it.
      setText(`[data-req="${id}"]`, live && req !== undefined && req !== cmd ? `requested ${word(id, req)}` : "");
    } else if (id.startsWith("level_switch_")) {
      const s = sig.levels && sig.levels[id];
      const overridden = (st.overrides || []).includes(id);
      let text = "Unknown", cls = "unknown";
      if (s && s.valid && s.reached !== null) { text = s.reached ? "Reached" : "Not reached"; cls = s.reached ? "reached" : "clear"; }
      if (!live) { text += " (stale)"; cls = "unknown"; }
      if (overridden) text += ` · OVERRIDE (actual ${st.override_actual[id] ? "reached" : "not reached"})`;
      setText(`[data-state="${id}"]`, text);
      for (const c of $$(`[data-switch="${id}"]`)) c.setAttribute("class", `sym switch ${cls}`);
    } else if (e.units === "°C") {
      const t = sig.temperatures && sig.temperatures[id];
      const ok = t && t.valid && t.celsius !== null;
      const text = ok ? `${t.celsius.toFixed(1)} °C` : "Unavailable";
      setText(`[data-value="${id}"]`, live ? text : `${text} (stale)`, ok && live ? "" : live ? "bad" : "bad stale");
      for (const b of $$(`[data-box="${id}"]`)) b.setAttribute("class", "value-box " + (ok && live ? "" : "bad"));
    } else if (id.startsWith("flow_sensor_")) {
      const f = sig.flows && sig.flows[id];
      const age = f && f.sampled_at != null ? st.now - f.sampled_at : null;
      const ft = flowText(f);
      const notes = [];
      if (ft.lpm && age !== null && age > FLOW_STALE_S) notes.push(`Stale ${age.toFixed(0)} s`);
      if (ft.unconfirmed) notes.push(UNCONFIRMED);
      if (ft.pulses && !ft.lpm) notes.push(`${ft.hz} pulses/s`);   // counts in the detail panel
      if (!live) notes.push("stale");
      // L/min unavailable is not a fault in the pulse input: amber, smaller text.
      setText(`[data-value="${id}"]`, ft.value, !live ? "bad" : ft.lpm ? "" : ft.pulses ? "uncal" : "bad");
      setText(`[data-detail="${id}"]`, notes.join(" · "));
    }
  }
  // Y4: requested, written and measured are three different things.
  const y4req = st.requested ? st.requested.control_valve_percent : null;
  const y4wr = st.written ? st.written.control_valve_percent : null;
  const fb = sig.valve && sig.valve.feedback;
  const lastKnown = live ? "" : " (last known)";
  setText('[data-y4="req"]', `Requested ${y4req == null ? "—" : y4req.toFixed(1) + " %"}${lastKnown}`, live ? "" : "stale");
  setText('[data-y4="wr"]', `${isShadow(st) ? "Not written — outputs disabled" : "Written " + (y4wr == null ? "not confirmed" : y4wr.toFixed(1) + " %")}${lastKnown}`, live ? "" : "stale");
  setText('[data-y4="fb"]', `Measured ${fb && fb.valid && fb.position_percent != null ? fb.position_percent.toFixed(1) + " %" : "Unavailable"}${lastKnown}`, live ? "" : "stale");
}

/* Context cards ------------------------------------------------------------ */
function treatmentWord(st) {
  const s = st.state;
  if (s === "TRIPPED") return ["Tripped", "crit"];
  if (s === "MODEL_TREATMENT" || s === "FALLBACK_TREATMENT") return st.release_permitted ? ["Treatment enabled", "ok"] : ["Opening (Y3 delay)", "wait"];
  if (s === "READY" || s === "FALLBACK_QUALIFY") return ["Qualifying", "wait"];
  if (s === "PREHEAT_3KW" || s === "HEAT_UP") return ["Heating, waiting to qualify", "wait"];
  if (s === "STOPPING") return ["Stopping", "wait"];
  return ["Waiting (idle)", "idle"];
}

function modelBlock(st) {
  const m = st.model || {};
  const rows = [];
  if (!m.enabled) rows.push(`<p class="muted">Model control disabled in the settings.</p>`);
  if (!m.status) {
    rows.push(`<p class="muted">Model unavailable: ${UI.esc(m.reason || "no result")}.</p>`);
  } else {
    const fmt = (v, u) => (v == null ? "—" : `${Number(v).toFixed(1)}${u}`);
    rows.push(`<table class="kv">
      <tr><th>Status</th><td>${UI.esc(m.status)}${m.usable ? "" : ` (not used: ${UI.esc(m.reason)})`}</td></tr>
      <tr><th>Prediction age</th><td>${fmt(m.age_s, " s")}</td></tr>
      <tr><th>PT3 predicted / measured</th><td>${fmt(m.predicted_pt3_c, " °C")} / ${fmt(m.measured_pt3_c, " °C")}</td></tr>
      <tr><th>Predicted log reduction</th><td>${m.predicted_log_reduction == null ? "—" : m.predicted_log_reduction.toFixed(2)}</td></tr></table>`);
  }
  if (st.path === "FALLBACK") rows.push(`<p class="note">Fallback control: no model-confirmed reduction.</p>`);
  return rows.join("");
}

function provisional(ids) {
  const c = APP.view && APP.view.config;
  if (!c) return "";
  const notes = [];
  if (!c.settings_validated) notes.push("Control settings PROVISIONAL");
  if (!c.physical_assignments_verified) notes.push("Physical assignments not verified");
  for (const id of ids) {
    if (id.startsWith("flow_sensor_") && c.flow_calibration_confirmed && !c.flow_calibration_confirmed[id]) notes.push(`${UI.tagOf(APP.meta, id)}: calibration not confirmed`);
    if (/^PT\d/.test(id) && !c.temperature_ranges_validated) notes.push("Temperature plausibility ranges unvalidated");
    if (id === "model" && !c.model_validated) notes.push("Treatment model not validated");
  }
  return [...new Set(notes)].map((n) => `<span class="flag">${UI.esc(n)}</span>`).join("");
}

function renderCard() {
  const st = APP.view && APP.view.status;
  const card = $("#card");
  if (APP.selected) return renderDetail();
  MANUAL.renderDetail(null);
  if (!st) { card.innerHTML = `<h2>Waiting for controller data</h2>`; return; }
  const live = isLive();
  if (APP.tab === "pasteurizer") {
    const [text, cls] = treatmentWord(st);
    const timers = [];
    if (st.dwell_remaining_s != null) timers.push(`Qualification: ${st.dwell_remaining_s.toFixed(0)} s remaining`);
    if (st.supply_wait_remaining_s != null) timers.push(`Supply lost: stopping in ${st.supply_wait_remaining_s.toFixed(0)} s`);
    if (st.stop_hold) timers.push("Held in IDLE by GUI STOP");
    if (!st.retry_available) timers.push("Low-flow retry used; restored after 10 min good flow or reset");
    card.innerHTML = `<h2>Treatment</h2>
      <div class="status-word ${live ? cls : "stale"}">${live ? text : "Unknown (stale data)"}</div>
      <p class="reason">${UI.esc(st.state_reason)}</p>
      ${timers.map((t) => `<p>${UI.esc(t)}</p>`).join("")}
      ${isShadow(st) ? `<p class="note">Outputs disabled (shadow run): no command is written, no treatment takes place.</p>` : ""}
      <h3>Model</h3>${modelBlock(st)}
      <div class="flags">${provisional(["model", "flow_sensor_3", "PT3_B4"])}</div>`;
  } else {
    const sig = st.signals || {};
    const lv = (n) => { const s = sig.levels && sig.levels[`level_switch_${n}`]; return s && s.valid ? (s.reached ? "reached" : "—") : "?"; };
    const flow = (n) => { const ft = flowText(sig.flows && sig.flows[`flow_sensor_${n}`]); return ft.pulses && !ft.lpm ? `${ft.value}<br><span class="muted small">${ft.pulses}</span>` : ft.value; };
    const cmd = (id) => {
      if (!live || !st.commanded) return "Unknown (stale)";
      const req = st.requested ? st.requested[id] : undefined;
      return `Command ${word(id, st.commanded[id])}${req !== undefined && req !== st.commanded[id] ? ` (requested ${word(id, req)})` : ""}` +
        (isShadow(st) ? `<br><span class="note small">${NOT_WRITTEN}</span>` : "");
    };
    card.innerHTML = `<h2>EDB</h2>
      <table class="kv">
        <tr><th colspan="2">Line 1 (Tank #1)</th></tr>
        <tr><td>EDB-PU-01 pump</td><td>${cmd("transfer_pump_1")}</td></tr>
        <tr><td>EDB-FR-01 flow</td><td>${flow(1)}</td></tr>
        <tr><td>Switches low / high / overflow</td><td>${lv(2)} / ${lv(4)} / ${lv(6)}</td></tr>
        <tr><td>EDB-VL-05 overflow Y1</td><td>${cmd("overflow_valve_1_open")}</td></tr>
        <tr><th colspan="2">Line 2 (Tank #2)</th></tr>
        <tr><td>EDB-PU-02 pump</td><td>${cmd("transfer_pump_2")}</td></tr>
        <tr><td>EDB-FR-02 flow</td><td>${flow(2)}</td></tr>
        <tr><td>Switches low / high / overflow</td><td>${lv(3)} / ${lv(5)} / ${lv(7)}</td></tr>
        <tr><td>EDB-VL-06 overflow Y2</td><td>${cmd("overflow_valve_2_open")}</td></tr>
        <tr><th colspan="2">Manhole</th></tr>
        <tr><td>EDB-PU-03 drain pump</td><td>${cmd("manhole_drain")}</td></tr>
        <tr><td>EDB-LS-01 water present</td><td>${lv(1)}</td></tr>
      </table>
      <p class="muted">${isShadow(st) ? "Shadow run: commands are computed from live inputs and not written to any output." : "Pump states are commands; a written ON does not prove a pump runs."}</p>
      <div class="flags">${provisional(["flow_sensor_1", "flow_sensor_2"])}</div>`;
  }
}

function renderDetail() {
  const st = APP.view && APP.view.status;
  const e = APP.meta.byId[APP.selected];
  if (!e) { APP.selected = null; return renderCard(); }
  const rows = [`<tr><th>Tag</th><td>${UI.esc(e.tag)}</td></tr>`, `<tr><th>Name</th><td>${UI.esc(e.name)}</td></tr>`];
  const kindText = {
    command: "Output command (no physical feedback)", "command+feedback": "Output command with measured position",
    measured: "Measurement", manual: "Manual — position not monitored", static: "Reference symbol",
    not_monitored: "No signal in the controller",
  };
  rows.push(`<tr><th>Kind</th><td>${kindText[e.kind] || e.kind}</td></tr>`);
  if (st && (e.kind === "command" || e.kind === "command+feedback")) {
    const v = (frame) => (frame ? (e.units === "%" ? `${Number(frame[e.id]).toFixed(1)} %` : word(e.id, frame[e.id])) : "not confirmed");
    rows.push(`<tr><th>Requested</th><td>${v(st.requested)}</td></tr>`,
      `<tr><th>Commanded</th><td>${v(st.commanded)}</td></tr>`,
      `<tr><th>Written</th><td>${writtenText(st, v)}</td></tr>`);
    if (e.kind === "command+feedback") {
      const fb = st.signals.valve && st.signals.valve.feedback;
      rows.push(`<tr><th>Measured feedback</th><td>${fb && fb.valid ? fb.position_percent.toFixed(1) + " %" : "Unavailable"}</td></tr>`);
    } else rows.push(`<tr><th>Feedback</th><td>None: a written command does not prove operation</td></tr>`);
  }
  if (st && e.kind === "measured") {
    const s = st.signals;
    const r = (s.temperatures && s.temperatures[e.id]) || (s.flows && s.flows[e.id]) || (s.levels && s.levels[e.id]);
    if (r && s.flows && s.flows[e.id]) {
      const ft = flowText(r);
      rows.push(`<tr><th>Pulse input</th><td>${r.valid ? "valid" : "invalid"}${r.error ? " · " + UI.esc(r.error) : ""}</td></tr>`,
        `<tr><th>Pulse counter</th><td>${r.raw_counter ?? "—"}</td></tr>`,
        `<tr><th>Pulse rate</th><td>${r.pulse_rate_hz == null ? "—" : r.pulse_rate_hz.toFixed(2) + " pulses/s"}</td></tr>`,
        `<tr><th>Flow</th><td>${ft.lpm ? ft.value : ft.unconfirmed ? `${ft.value} — ${UNCONFIRMED}` : r.valid ? "L/min unavailable — calibration pending (pulses per litre not set)" : "Unavailable"}</td></tr>`);
    } else if (r) {
      const value = r.celsius ?? r.flow_lpm ?? (r.reached === true ? "Reached" : r.reached === false ? "Not reached" : null);
      rows.push(`<tr><th>Value</th><td>${r.valid && value != null ? (typeof value === "number" ? value.toFixed(2) + " " + (e.units || "") : value) : "Unavailable"}</td></tr>`,
        `<tr><th>Valid</th><td>${r.valid ? "yes" : "no"}${r.error ? " · " + UI.esc(r.error) : ""}</td></tr>`);
    }
  }
  const blocks = st ? (st.arbitration_notes || []).filter((n) => n.output === e.id) : [];
  const alarms = st ? (st.alarms || []).filter((a) => (a.equipment || []).includes(e.id)) : [];
  $("#card").innerHTML = `<div class="detail-head"><h2>${UI.esc(e.tag)}</h2><button id="close-detail">Close</button></div>
    <table class="kv">${rows.join("")}</table>
    <h3>Blocking interlocks</h3>${blocks.length ? `<ul>${blocks.map((n) => `<li><b>${UI.esc(n.rule)}</b>: ${UI.esc(n.message)}</li>`).join("")}</ul>` : `<p class="muted">None this cycle.</p>`}
    <h3>Related alarms</h3>${alarms.length ? `<ul>${alarms.map((a) => `<li>${UI.severity(a.severity)} ${UI.esc(a.code)}: ${UI.esc(a.message)}</li>`).join("")}</ul>` : `<p class="muted">None.</p>`}
    <div class="flags">${provisional([e.id])}</div>
    ${isLive() ? "" : `<p class="note">Data is stale: values are the last received.</p>`}`;
  $("#close-detail").onclick = () => { APP.selected = null; renderCard(); };
  MANUAL.renderDetail(e.id);   // manual buttons for this part, only in MANUAL
}

/* Alarms ------------------------------------------------------------------- */
function renderAlarms() {
  const st = APP.view && APP.view.status;
  const body = $("#alarm-rows");
  const alarms = st ? [...st.alarms] : [];
  alarms.sort((a, b) => (b.level - a.level) || String(b.raised_utc).localeCompare(String(a.raised_utc)));
  const live = isLive();
  $("#alarm-title").textContent = live ? "Active alarms" : "Last known alarms";
  $("#alarm-count").textContent = !st ? "" : live ? `${alarms.length} active or latched`
    : `${alarms.length} active or latched at the last snapshot · controller data not current, alarms may have changed`;
  $("#alarm-panel").classList.toggle("last-known", !live);
  if (!alarms.length) {
    body.innerHTML = `<tr><td colspan="7" class="muted">${!st ? "No controller data." : live ? "No active alarms." : "No alarms at the last snapshot."}</td></tr>`;
    return;
  }
  const canAck = SESSION.user && live;
  body.innerHTML = alarms.map((a) => `<tr class="${a.severity === "CRITICAL" ? "row-critical" : "row-warning"}${live ? "" : " row-stale"}">
    <td>${UI.severity(a.severity)}</td><td>${UI.time(a.raised_utc)}</td>
    <td>${(a.equipment || []).map((id) => UI.esc(UI.tagOf(APP.meta, id))).join(", ") || "Plant"}</td>
    <td><b>${UI.esc(a.code)}</b> ${UI.esc(a.message)}</td><td>${UI.esc(a.consequence)}</td>
    <td>${live ? "" : "Last known: "}${alarmStatus(a)} · ${a.acknowledged ? "Acknowledged" : "Not acknowledged"}</td>
    <td>${canAck && !a.acknowledged ? `<button class="secondary small-btn" data-ack="${UI.esc(a.code)}">Acknowledge</button>` : ""}</td></tr>`).join("");
  for (const b of $$("[data-ack]", body)) b.onclick = () => MANUAL.send("acknowledge", { code: b.dataset.ack });
}

function alarmStatus(a) {
  if (a.active) return "Active";
  // Latching alarms (level 2 and 3) stop equipment until reset: the cause
  // cannot be re-checked while stopped, so "cleared" would claim too much.
  if (a.level >= 2) return "Latched — reset required; cause not verified while stopped";
  return "Condition no longer detected";
}

/* Main loop ------------------------------------------------------------------ */
async function poll() {
  try {
    APP.view = await UI.get("/api/live");
    $("#gui-error").textContent = "";
  } catch (error) {
    APP.view = { state: "DISCONNECTED", age_s: null, status: APP.view && APP.view.status, config: APP.view && APP.view.config };
    $("#gui-error").textContent = `GUI server unreachable: ${error.message}`;
  }
  renderHeader();
  renderEquipment();
  renderCard();
  renderAlarms();
  MANUAL.render();
  $("#footer-config").textContent = APP.view.status ? `config ${APP.view.status.config_hash.slice(0, 12)} · run ${APP.view.status.boot_id.slice(0, 8)}` : "";
}

function showTab(tab) {
  APP.tab = tab;
  APP.selected = null;
  for (const b of $$("nav [data-tab]")) b.classList.toggle("active", b.dataset.tab === tab);
  $("#view-edibl").hidden = tab !== "edibl";
  $("#view-pasteurizer").hidden = tab !== "pasteurizer";
  $("#view-trends").hidden = tab !== "trends";
  $("#view-manual").hidden = tab !== "manual";
  $("#side").hidden = tab === "trends" || tab === "manual";
  document.querySelector("main").classList.toggle("wide", tab === "trends" || tab === "manual");
  if (tab === "trends") TRENDS.show();
  renderCard();
}

async function init() {
  APP.meta = UI.indexMeta(await UI.get("/api/meta"));
  $("#view-edibl").innerHTML = VIEWS.edibl() + VIEWS.legendEdibl();
  $("#view-pasteurizer").innerHTML = VIEWS.pasteurizer();
  for (const g of $$(".equip")) {
    const open = () => { APP.selected = g.dataset.id; renderCard(); };
    g.addEventListener("click", open);
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter") open(); });
  }
  for (const b of $$("nav [data-tab]")) b.onclick = () => showTab(b.dataset.tab);
  $("#open-log").onclick = () => window.open("/log", "mwt-biogas-log", "width=1200,height=800");
  TRENDS.init();
  MANUAL.init();
  showTab("edibl");
  await poll();
  setInterval(poll, 1000);
}

init();
