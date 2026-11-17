/* Solar tab: PV array, two Deye inverters, battery and load, and the solar
   permission of the electric heaters, from /api/solar.

   The controller is the only Modbus client: it reads the inverters every
   5 s, decides the heater permission (pasteurizer/energy.py) and publishes
   both in its status; the GUI server passes them on. Without a controller
   snapshot there is nothing to show. Totals cover fresh inverters only: a
   missing inverter is never counted as zero (PARTIAL). Moving dashes show
   the direction of power from the readings; grey dotted lines mean no
   current reading. Colour never carries meaning alone: every value also
   has text. The biogas heating is not gated by the solar permission. */
"use strict";

const SOLAR = (() => {
  const C = { pv: "#d4a106", batt: "#2f9e57", ac: "#2f7fd1" };
  const POLL_MS = 5000;
  const W = 1400, H = 600;
  const FLOW_MIN_W = 10;                 // below this a line is drawn idle
  const S = { view: null, error: "", built: null };

  const q = (sel, root = document) => root.querySelector(sel);
  const qa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (t) => UI.esc(t);
  const kw = (w) => (w == null ? "—" : `${(w / 1000).toFixed(2)} kW`);
  const num = (v, digits, unit) => (v == null ? "—" : `${Number(v).toFixed(digits)} ${unit}`);
  const dirWord = { DISCHARGING: "discharging", CHARGING: "charging", IDLE: "idle" };

  /* Drawing ------------------------------------------------------------------ */
  const line = (pts, colour, flow) =>
    `<polyline class="solar-flow flow-unknown" data-flow="${flow}" points="${pts.map((p) => p.join(",")).join(" ")}" stroke="${colour}"/>`;
  const text = (x, y, content, cls, anchor = "middle", key = "") =>
    `<text class="${cls}" x="${x}" y="${y}" text-anchor="${anchor}"${key ? ` data-solar="${key}"` : ""}>${esc(content)}</text>`;

  // Inverter k (0 or 1): box position and the points where the lines enter.
  const SLOT = [
    { y: 50, pv: [[330, 110], [560, 110]],
      batt: [[300, 410], [480, 410], [480, 388]], batt2: [[480, 372], [480, 190], [560, 190]],
      ac: [[860, 140], [980, 140], [980, 255], [1100, 255]] },
    { y: 320, pv: [[330, 170], [420, 170], [420, 380], [560, 380]],
      batt: [[300, 470], [560, 470]], batt2: null,
      ac: [[860, 410], [980, 410], [980, 305], [1100, 305]] },
  ];

  function inverterBox(inv, k) {
    const y = SLOT[k].y, x = 560, id = inv.id;
    return `<g class="solar-inv"><title>${esc(inv.name)} · Deye 5 kW hybrid · ${esc(inv.host)}:${inv.port}</title>
      <rect class="inv-box" x="${x}" y="${y}" width="300" height="180" rx="8"/>
      <circle class="lamp unknown" data-solar="${id}-lamp" cx="${x + 18}" cy="${y + 20}" r="8"/>
      ${text(x + 34, y + 25, inv.name, "tag", "start")}${text(x + 286, y + 25, "Deye 5 kW hybrid", "name", "end")}
      ${text(x + 14, y + 50, "—", "state", "start", `${id}-state`)}
      ${text(x + 14, y + 76, "PV —", "solar-val", "start", `${id}-pv`)}
      ${text(x + 14, y + 100, "Battery —", "solar-val", "start", `${id}-batt`)}
      ${text(x + 14, y + 124, "Load —", "solar-val", "start", `${id}-load`)}
      ${text(x + 14, y + 148, "SOC —", "solar-val", "start", `${id}-soc`)}
      ${text(x + 14, y + 170, `${inv.host}:${inv.port} · ID ${inv.device_id}`, "name", "start")}</g>`;
  }

  function drawing(inverters) {
    const shown = inverters.slice(0, 2);
    const lines = shown.map((inv, k) => {
      const s = SLOT[k];
      // Each line runs from source to sink: forward dashes = power in that direction.
      let out = line(s.pv, C.pv, `pv:${inv.id}`) + line(s.ac, C.ac, `ac:${inv.id}`);
      // Inverter 1's battery line jumps over inverter 2's PV line (no connection).
      out += line(s.batt, C.batt, `batt:${inv.id}`);
      if (s.batt2) out += (shown.length > 1 ? `<path class="solar-jump" d="M480 388 a8 8 0 0 1 0 -16" stroke="${C.batt}"/>`
        : line([[480, 388], [480, 372]], C.batt, `batt:${inv.id}`)) + line(s.batt2, C.batt, `batt:${inv.id}`);
      return out;
    }).join("");
    const cells = [];
    for (let r = 0; r < 3; r++) for (let c = 0; c < 4; c++)
      cells.push(`<rect class="pv-cell" x="${106 + c * 56}" y="${76 + r * 41}" width="50" height="35" rx="2"/>`);
    const rays = [0, 45, 90, 135, 180, 225, 270, 315].map((a) => {
      const r = (a * Math.PI) / 180;
      return `<line class="sun" x1="${62 + 20 * Math.cos(r)}" y1="${42 + 20 * Math.sin(r)}" x2="${62 + 28 * Math.cos(r)}" y2="${42 + 28 * Math.sin(r)}"/>`;
    }).join("");
    return `<svg viewBox="0 0 ${W} ${H}" class="solar" role="img" aria-label="Solar system view">
      ${lines}
      <g class="solar-pv"><title>PV array (PV1 and PV2 strings of both inverters)</title>
        <circle class="sun" cx="62" cy="42" r="13"/>${rays}
        <rect class="pv-frame" x="100" y="70" width="230" height="130" rx="4"/>${cells.join("")}
        <line class="sym" x1="215" y1="200" x2="215" y2="214"/><line class="sym" x1="185" y1="214" x2="245" y2="214"/>
        ${text(215, 238, "PV array", "tag")}
        ${text(215, 262, "Total PV —", "solar-total", "middle", "pv-total")}
        ${text(215, 280, "", "note", "middle", "pv-note")}</g>
      <g class="solar-batt"><title>Battery (shared by both inverters)</title>
        <rect class="sym" x="150" y="370" width="24" height="10" rx="2"/><rect class="sym" x="246" y="370" width="24" height="10" rx="2"/>
        <rect class="batt-fill" data-solar="batt-fill" x="126" y="514" width="168" height="0"/>
        <rect class="batt-body" x="120" y="380" width="180" height="140" rx="8"/>
        ${text(210, 438, "SOC —", "solar-total", "middle", "batt-soc")}
        ${text(210, 462, "—", "name", "middle", "batt-volt")}
        ${text(210, 544, "Battery", "tag")}
        ${text(210, 568, "—", "solar-total", "middle", "batt-power")}
        ${text(210, 588, "", "note", "middle", "batt-note")}</g>
      <g class="solar-load"><title>Electrical load supplied by the inverters</title>
        <rect class="inv-box" x="1100" y="200" width="220" height="160" rx="8"/>
        <path class="bolt" d="M1218 222 l-22 34 h16 l-8 28 l26 -38 h-16 z"/>
        ${text(1210, 310, "Electrical load", "tag")}
        ${text(1210, 334, "Total load —", "solar-total", "middle", "load-total")}
        ${text(1210, 352, "", "note", "middle", "load-note")}</g>
      ${shown.map(inverterBox).join("")}
      ${inverters.length > 2 ? text(710, 540, `${inverters.length - 2} more inverter(s): see the table`, "note") : ""}
    </svg>
    <div class="legend-strip"><b>Legend:</b>
      <span><i style="background:${C.pv}"></i>PV (DC)</span>
      <span><i style="background:${C.batt}"></i>Battery (DC)</span>
      <span><i style="background:${C.ac}"></i>Load (AC)</span>
      <span>Moving dashes: direction of power from the readings · grey dotted: no current reading</span></div>`;
  }

  function skeleton() {
    return `<div class="solar-head">
        <span id="solar-complete" class="chip">Solar: waiting for data…</span>
        <span id="solar-updated" class="muted"></span></div>
      <div id="solar-error" class="gui-error"></div>
      <section class="energy-panel">
        <h3 class="solar-h3">Electrical heating (solar permission)</h3>
        <div id="energy-note" class="muted energy-note"></div>
        <div class="energy-tiles" id="energy-tiles"></div>
        <table class="solar-table energy-table"><thead><tr><th>Heaters</th><th>Power</th>
          <th>Start needs</th><th>Stops when</th><th>Surplus share</th><th>Permission</th><th>Reason</th></tr></thead>
          <tbody id="energy-modes"><tr><td colspan="7" class="muted">Waiting for data…</td></tr></tbody></table>
        <div class="energy-actions" id="energy-actions" hidden>
          <button type="button" id="energy-retry">Retry now</button>
          <span id="energy-retry-note" class="small muted"></span></div>
        <table class="solar-table energy-table"><thead><tr><th>Heater</th><th>Requested</th>
          <th>Commanded</th><th>Written</th><th>Blocked by</th></tr></thead>
          <tbody id="energy-heaters"></tbody></table>
      </section>
      <div id="solar-drawing"></div>
      <h3 class="solar-h3">Inverters</h3>
      <table class="solar-table"><thead><tr><th>Inverter</th><th>Address</th><th>Status</th><th>Last good read</th>
        <th>PV1</th><th>PV2</th><th>PV total</th><th>Battery V</th><th>SOC</th><th>Battery power</th><th>Load</th><th>Last error</th></tr></thead>
        <tbody id="solar-rows"><tr><td colspan="12" class="muted">Waiting for data…</td></tr></tbody></table>`;
  }

  /* Values ------------------------------------------------------------------- */
  function setText(key, value, cls) {
    for (const el of qa(`[data-solar="${key}"]`)) {
      el.textContent = value;
      if (cls !== undefined) el.setAttribute("class", cls);
    }
  }

  function setFlow(key, mode) {
    for (const el of qa(`[data-flow="${key}"]`)) el.setAttribute("class", `solar-flow flow-${mode}`);
  }

  function stateText(inv, reachable) {
    if (!reachable) return "Unknown (GUI server unreachable)";
    const age = inv.age_s == null ? "" : ` · ${inv.age_s.toFixed(0)} s old`;
    return {
      OK: `OK${age}${inv.consecutive_failures ? ` · last read failed` : ""}`,
      STALE: `STALE — no new reading${age}`,
      OFFLINE: inv.age_s == null ? "OFFLINE — no reading yet" : `OFFLINE — last reading${age}`,
      WAITING: "Waiting for the first read",
      UNAVAILABLE: "UNAVAILABLE — pymodbus not installed",
    }[inv.state] || inv.state;
  }

  function batteryText(w, dir, verified) {
    if (w == null) return "—";
    const word = dirWord[dir] || "";
    return `${kw(Math.abs(w))} ${word}${dir === "CHARGING" && !verified ? " (sign unverified)" : ""}`;
  }

  function render() {
    const body = S.view;
    const ctl = body && body.controller;
    const linkOk = !S.error && ctl && ctl.state === "CONNECTED";
    const reachable = linkOk;
    q("#solar-error").textContent = S.error ||
      (ctl && ctl.state !== "CONNECTED" ? `Controller ${ctl.state}: values below are the last received` +
        (ctl.error ? ` (${ctl.error})` : "") : "");
    if (!body) return;
    renderEnergy(body, linkOk);
    const v = body.solar;
    const head = q("#solar-complete");
    if (!v) {
      head.textContent = ctl && ctl.state === "WAITING" ? "Solar: waiting for the controller"
        : "Solar: no data (controller not running, or no solar data in its status)";
      head.className = "chip chip-warn";
      q("#solar-drawing").innerHTML = "";
      q("#solar-rows").innerHTML = `<tr><td colspan="12" class="muted">The controller reads the inverters; no data.</td></tr>`;
      S.built = null;
      return;
    }
    if (!v.enabled) {
      head.textContent = `Solar monitoring off: ${v.reason || "disabled"}`;
      head.className = "chip chip-warn";
      q("#solar-drawing").innerHTML = "";
      q("#solar-rows").innerHTML = `<tr><td colspan="12" class="muted">${esc(v.reason || "Solar monitoring disabled.")}</td></tr>`;
      S.built = null;
      return;
    }
    const ids = v.inverters.map((i) => i.id).join(",");
    if (S.built !== ids) { q("#solar-drawing").innerHTML = drawing(v.inverters); S.built = ids; }
    q("#solar-updated").textContent = `GUI time ${UI.time(new Date(body.gui_time * 1000).toISOString())} · ` +
      `controller reads every ${v.poll_s} s, stale after ${v.stale_after_s} s${v.simulated ? " · SIMULATED inverters" : ""}`;

    const t = v.totals;
    const fresh = (inv) => reachable && inv.fresh;
    const nFresh = v.inverters.filter(fresh).length;
    const partial = !reachable || !t.complete;
    if (!reachable) { head.textContent = "Solar: last known values (GUI server unreachable)"; head.className = "chip chip-warn"; }
    else if (!nFresh) { head.textContent = "Solar: NO CURRENT DATA — all inverters unavailable"; head.className = "chip chip-warn"; }
    else if (partial) { head.textContent = `Solar: PARTIAL — ${t.missing.join(", ")} unavailable; totals cover ${t.included.join(", ")} only`; head.className = "chip chip-warn"; }
    else { head.textContent = `Solar: all ${nFresh} inverters current`; head.className = "chip chip-ok"; }
    document.body.classList.toggle("solar-stale", !reachable || !nFresh);

    // Totals: never computed in the browser, only shown (backend sums fresh inverters).
    const tag = partial && nFresh ? " (partial)" : "";
    const totalCls = (ok) => (ok ? "solar-total" : "solar-total stale");
    const show = reachable && nFresh;
    setText("pv-total", `Total PV ${show ? kw(t.pv_w) : "—"}${show ? tag : ""}`, totalCls(show));
    setText("load-total", `Total load ${show ? kw(t.load_w) : "—"}${show ? tag : ""}`, totalCls(show));
    setText("batt-power", show ? `${batteryText(t.battery_w, t.battery_direction, v.battery_charge_sign_verified)}${tag}` : "—", totalCls(show));
    const missingNote = partial && nFresh ? `without ${t.missing.join(", ")}` : !show ? "no current reading" : "";
    setText("pv-note", missingNote); setText("load-note", missingNote);
    setText("batt-note", v.soc_mismatch && show ? `SOC values differ by more than ${v.soc_mismatch_pct} %` : missingNote);

    // Battery: SOC per inverter (never added); fill shows the lowest current SOC.
    const socs = v.inverters.filter(fresh).map((i) => i.reading.battery_soc_pct);
    const socText = v.inverters.map((i) => (fresh(i) ? `${i.reading.battery_soc_pct} %` : "—")).join(" / ");
    setText("batt-soc", socs.length ? `SOC ${socText}` : "SOC —", totalCls(socs.length > 0));
    const volts = v.inverters.filter(fresh).map((i) => `${i.reading.battery_voltage_v.toFixed(2)} V`);
    setText("batt-volt", volts.length ? volts.join(" / ") : "—");
    const fill = q('[data-solar="batt-fill"]');
    if (fill) {
      const soc = socs.length ? Math.max(0, Math.min(100, Math.min(...socs))) : 0;
      const h = (128 * soc) / 100;
      fill.setAttribute("height", h.toFixed(1)); fill.setAttribute("y", (514 - h).toFixed(1));
    }

    for (const inv of v.inverters) {
      const r = inv.reading, ok = fresh(inv), cls = ok ? "solar-val" : "solar-val stale";
      for (const el of qa(`[data-solar="${inv.id}-lamp"]`))
        el.setAttribute("class", "lamp " + (ok ? "on" : inv.state === "WAITING" && reachable ? "unknown" : "fault"));
      setText(`${inv.id}-state`, stateText(inv, reachable), ok ? "state" : "state bad");
      const last = "";   // not current: greyed, and the state line gives the age
      setText(`${inv.id}-pv`, r ? `PV ${kw(r.pv_total_w)} (${kw(r.pv1_power_w)} + ${kw(r.pv2_power_w)})${last}` : "PV —", cls);
      setText(`${inv.id}-batt`, r ? `Battery ${batteryText(r.battery_power_w, r.battery_direction, v.battery_charge_sign_verified)}${last}` : "Battery —", cls);
      setText(`${inv.id}-load`, r ? `Load ${kw(r.load_power_w)}${last}` : "Load —", cls);
      setText(`${inv.id}-soc`, r ? `SOC ${r.battery_soc_pct} % · ${r.battery_voltage_v.toFixed(2)} V${last}` : "SOC —", cls);
      // Flow direction: lines are drawn source -> inverter -> load.
      setFlow(`pv:${inv.id}`, !ok ? "unknown" : r.pv_total_w >= FLOW_MIN_W ? "fwd" : "idle");
      setFlow(`ac:${inv.id}`, !ok ? "unknown" : r.load_power_w >= FLOW_MIN_W ? "fwd" : "idle");
      setFlow(`batt:${inv.id}`, !ok ? "unknown" : r.battery_direction === "DISCHARGING" ? "fwd" : r.battery_direction === "CHARGING" ? "rev" : "idle");
    }

    q("#solar-rows").innerHTML = v.inverters.map((inv) => {
      const r = inv.reading, ok = fresh(inv), d = (x) => (r ? x : "—");
      return `<tr class="${ok ? "" : "row-stale"}">
        <td><b>${esc(inv.name)}</b></td><td>${esc(inv.host)}:${inv.port} · ID ${inv.device_id}</td>
        <td class="${ok ? "" : "bad-text"}">${esc(stateText(inv, reachable))}</td>
        <td class="nowrap">${inv.last_ok_utc ? UI.time(inv.last_ok_utc) : "never"}</td>
        <td>${d(kw(r && r.pv1_power_w))}</td><td>${d(kw(r && r.pv2_power_w))}</td><td>${d(kw(r && r.pv_total_w))}</td>
        <td>${d(num(r && r.battery_voltage_v, 2, "V"))}</td><td>${d(r && `${r.battery_soc_pct} %`)}</td>
        <td>${d(r && batteryText(r.battery_power_w, r.battery_direction, v.battery_charge_sign_verified))}</td>
        <td>${d(kw(r && r.load_power_w))}</td>
        <td class="small">${esc(inv.error || "")}</td></tr>`;
    }).join("");
  }

  /* Electrical heating panel ------------------------------------------------ */
  const MODE_LABEL = { heater_1kw: "1 kW", heater_3kw: "3 kW" };
  const HEATER_LABEL = { heater_1kw: "1 kW assist heater (EH-02)", heater_3kw: "3 kW startup heater (EH-01)" };
  const W_ = (w) => (w == null ? "—" : `${Math.round(w)} W`);
  const yesNo = (b) => (b == null ? "—" : b ? "on" : "off");

  function tile(label, value, sub, cls = "") {
    return `<div class="energy-tile ${cls}"><div class="muted">${esc(label)}</div>
      <div class="energy-value">${esc(value)}</div><div class="small muted">${esc(sub || "")}</div></div>`;
  }

  function renderEnergy(body, current) {
    const e = body.energy, set = body.settings || {};
    const note = q("#energy-note");
    if (!e) {
      note.textContent = "No energy data from the controller.";
      q("#energy-tiles").innerHTML = "";
      q("#energy-modes").innerHTML = `<tr><td colspan="7" class="muted">—</td></tr>`;
      q("#energy-heaters").innerHTML = "";
      renderRetry(null);
      return;
    }
    const old = current ? "" : " (last received)";
    note.textContent = !e.enabled
      ? "Solar permission switched off in arbiter_settings.json: electric heaters follow the other interlocks only."
      : `Single heater: with a heat request, current data, SOC ≥ ${set.soc_start_min_percent} % and PV ≥ ` +
        `${set.probe_min_pv_w} W it is switched on as a test; ${set.probe_settle_s} s after it is written on, the next ` +
        `reading of each inverter must show ≥ ${pct(set.probe_min_solar_contribution)} solar contribution and ≤ ` +
        `${set.probe_max_battery_discharge_w} W battery discharge. Failed or aborted test: next one after ` +
        `${Math.round(set.probe_retry_delay_s / 60)} min. Running: off when too low / too high for ${set.probe_hold_s} s, ` +
        `at SOC ≤ ${set.soc_stop_at_percent} % at once, or after ${set.communication_timeout_s} s without data. ` +
        `Biogas heating is not affected.`;

    const requested = Object.entries(body.heaters || {}).filter(([, h]) => h.requested).map(([n]) => n);
    const basis = requested.length ? requested : (e.requests && e.requests.length ? e.requests : e.heaters_on);
    const basisW = basis.reduce((sum, n) => sum + ((set.heater_power_w || {})[n] || 0), 0);
    const contribution = e.surplus_w != null && basisW ? `${Math.round((100 * e.surplus_w) / basisW)} %` : "—";
    const contribSub = basisW ? `of ${basis.map((n) => MODE_LABEL[n]).join(" + ")} ${requested.length || (e.requests || []).length ? "requested" : "on"}` : "no electric heater requested";
    const data = e.data_valid ? "inverter data current"
      : `NO VALID DATA${e.data_age_s != null ? ` for ${Math.round(e.data_age_s)} s` : ""}: ${e.data_reason}`;
    const soc = e.soc_percent == null ? "—" : `${Math.round(e.soc_percent)} %`;
    const socSub = e.soc_each && e.soc_each.length > 1 ? `lower of ${e.soc_each.map((x) => `${x} %`).join(" / ")}` : "";
    q("#energy-tiles").innerHTML =
      tile("Solar surplus" + old, W_(e.surplus_w),
           e.surplus_w == null ? data : `PV ${W_(e.pv_w)} − other loads ${W_(e.other_loads_w)} ` +
           `(load ${W_(e.load_w)} − heaters on ${W_(e.heater_on_w)})`, e.data_valid ? "" : "stale") +
      tile("Solar contribution", contribution, contribSub) +
      tile("Battery SOC", soc, socSub, e.soc_percent != null && e.soc_percent <= set.soc_stop_at_percent ? "bad" : "") +
      tile("Available heating power", e.available_heating_w ? `${(e.available_heating_w / 1000).toFixed(0)} kW` : "0 kW",
           e.available_heating_w ? "largest permitted heater set" : "no electric heating permitted",
           e.available_heating_w ? "good" : "bad") +
      tile("Battery balance (diagnostic)", e.balance_residual_w == null ? "—" : `${e.balance_residual_w} W`,
           `PV + battery − load; ${e.balance_ok === false ? "OUTSIDE tolerance: check battery sign/summing" : "within tolerance"}` +
           ` (± max(${set.battery_balance_tolerance_w} W, ${pct(set.battery_balance_tolerance_fraction)} of load))`,
           e.balance_ok === false ? "bad" : "") +
      (set.preheat_heating_s != null ? tile("3 kW preheat heating time", `${(set.preheat_heating_s / 60).toFixed(0)} min`,
           `trips at ${(set.preheat_max_heating_s / 3600).toFixed(1)} h of heating`) : "");

    const clock = (s) => { s = Math.max(0, Math.ceil(s)); return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s} s`; };
    const stateText = (m) => {
      const t = m.timer_s != null ? ` (${clock(m.timer_s)})` : "";
      return { PERMITTED: "PERMITTED", STOPPING: `STOPPING${t}`,
               BLOCKED: "BLOCKED", DISABLED: "RULE OFF",
               PROBING: `TEST ${m.phase || ""}${t}`, RETRY_WAIT: `RETRY${t}` }[m.state] || m.state;
    };
    const lastProbe = (m) => {
      const last = m.probe && m.probe.last;
      if (!last || !last.result) return "";
      const res = last.balance_residual_w == null ? "" :
        ` · balance PV + battery − load ${Math.round(last.balance_residual_w)} W${last.balance_ok === false ? " (outside tolerance: check battery sign/sum)" : ""}`;
      return `<div class="small muted">Last: ${esc(last.result)} — ${esc(last.reason)}${esc(res)}</div>`;
    };
    const needs = () =>
      [`test: ≥ ${pct(set.probe_min_solar_contribution)} solar, ≤ ${W_(set.probe_max_battery_discharge_w)} battery`,
       `same, ${set.probe_hold_s} s`];
    q("#energy-modes").innerHTML = ["heater_1kw", "heater_3kw"].map((key) => {
      const m = (e.modes || {})[key];
      if (!m) return "";
      const cls = m.permitted ? "good-text" : "bad-text";
      const [start, stop] = needs(key, m);
      return `<tr><td><b>${MODE_LABEL[key]}</b></td><td>${W_(m.power_w)}</td><td>${start}</td>
        <td>${stop}</td><td>${m.contribution_percent == null ? "—" : `${Math.round(m.contribution_percent)} %`}</td>
        <td class="${cls}"><b>${esc(stateText(m))}</b></td><td class="wrap">${esc(m.reason)}${m.timer_s != null && m.state !== "BLOCKED" ? ` ${clock(m.timer_s)}` : ""}${lastProbe(m)}</td></tr>`;
    }).join("") + (e.modes ? `<tr><td colspan="7" class="small muted">1 kW + 3 kW together: not permitted
      (no combined solar rule yet). The preheat exclusion also removes the 1 kW while the 3 kW is requested.</td></tr>` : "");

    renderRetry(e);

    q("#energy-heaters").innerHTML = Object.entries(body.heaters || {}).map(([name, h]) => {
      const notes = (h.notes || []).map((n) => `${n.rule}: ${n.message}`).join("; ");
      const blocked = h.requested && !h.commanded;
      return `<tr><td><b>${HEATER_LABEL[name] || name}</b></td><td>${yesNo(h.requested)}</td>
        <td class="${blocked ? "bad-text" : ""}">${yesNo(h.commanded)}</td><td>${yesNo(h.written)}</td>
        <td class="wrap ${blocked ? "bad-text" : ""}">${esc(notes || (h.requested ? "" : "not requested by the controller"))}</td></tr>`;
    }).join("");
  }

  const pct = (f) => (f == null ? "—" : `${Math.round(f * 100)} %`);

  /* "Retry now": COMMISSIONING users end the 10 min probe retry delay (AUTO
     and MANUAL). Only the waiting is bridged; the controller still checks
     the heat request, inverter data, start SOC, PV and interlocks. */
  let retryBusy = false;
  function canCommand() {
    return typeof SESSION !== "undefined" && SESSION.user && SESSION.role === "COMMISSIONING" && SESSION.commands;
  }
  function renderRetry(e) {
    const box = q("#energy-actions");
    box.hidden = !canCommand();
    if (box.hidden) return;
    const waiting = ["heater_1kw", "heater_3kw"].filter((k) => ((e && e.modes) || {})[k] &&
      e.modes[k].state === "RETRY_WAIT");
    const btn = q("#energy-retry");
    btn.disabled = retryBusy || !waiting.length;
    btn.title = waiting.length ? `End the retry delay of the ${waiting.map((k) => MODE_LABEL[k]).join(" + ")} heater now`
      : "No heater is waiting for a probe retry";
  }
  async function retryNow() {
    const note = q("#energy-retry-note");
    retryBusy = true; q("#energy-retry").disabled = true;
    try {
      const r = await UI.post("/api/command", { action: "energy_retry", args: {} });
      note.textContent = `${r.accepted ? "Accepted" : "Refused"}: ${r.reason}`;
      note.className = `small ${r.accepted ? "good-text" : "bad-text"}`;
    } catch (error) {
      note.textContent = `Command failed: ${error.message}`;
      note.className = "small bad-text";
    }
    retryBusy = false;
    poll();
  }

  async function poll() {
    try {
      S.view = await UI.get("/api/solar");
      S.error = "";
    } catch (error) {
      S.error = `GUI server unreachable: ${error.message} (values below are the last received)`;
    }
    render();
  }

  function init() {
    q("#view-solar").innerHTML = skeleton();
    q("#energy-retry").onclick = retryNow;
    poll();
    setInterval(poll, POLL_MS);
  }

  return { init, show: poll };
})();
