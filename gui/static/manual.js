/* Login session and the manual panel.

   Commands go to POST /api/command; the GUI server checks the login, the
   controller checks role, selector and interlocks again and logs every
   command. Manual requests expire in the controller 30 s after they were last
   renewed: this page renews its own requests every 10 s while it is open and
   connected. A renewal can only extend a request that is still active; after
   an expiry, a clear, leaving MANUAL or a controller restart it is refused and
   the request has to be pressed again. Closing the page withdraws this user's
   requests (best effort; if that message is lost they expire), and logging out
   or a session expiry withdraws them in the GUI server. Overrides expire after
   10 s and are renewed every 4 s. */
"use strict";

const SESSION = { user: null, role: null, logins: false, commands: false };

const MANUAL = (() => {
  const RENEW_MS = 10000;
  const OVERRIDE_RENEW_MS = 4000;
  const wanted = {};          // command -> { value, sent }
  const overrides = {};       // switch -> { value, sent }
  let valveTest = null;       // { sent } while the Y3/Y4 test allowance is wanted
  let structure = "";
  let message = { text: "", ok: true };

  const st = () => APP.view && APP.view.status;
  const cfg = () => (APP.view && APP.view.config) || {};
  const isValve = (cmd) => cmd.endsWith("_open");
  const DRAIN = "manhole_drain";      // no manual run: STOP inhibits, release = automatic
  const tag = (id) => (APP.meta && APP.meta.byId[id]) || { tag: id, name: "" };
  const fmt = (cmd, value) => (cmd === "control_valve_percent"
    ? (value == null ? "—" : `${Number(value).toFixed(1)} %`) : word(cmd, value));
  const wantedText = (cmd, value) => (cmd === DRAIN ? (value === false ? "STOP (automatic inhibited)" : "Automatic")
    : fmt(cmd, value));

  /* Why manual commands are not possible right now ([] = possible). */
  function blockers() {
    const s = st(), reasons = [];
    if (!SESSION.logins) reasons.push("Logins are not set up on this GUI (tools/manage_users.py).");
    else if (!SESSION.user) reasons.push("Log in with a COMMISSIONING user.");
    else if (SESSION.role !== "COMMISSIONING") reasons.push(`Your role ${SESSION.role} cannot request equipment (COMMISSIONING needed).`);
    if (!SESSION.commands) reasons.push("The controller's command channel is not available.");
    if (!isLive()) reasons.push("No current controller data.");
    else if (s && s.mode !== "MANUAL") reasons.push(`Selector S1 is in ${s.mode}: turn it to MANUAL.`);
    return reasons;
  }

  async function send(action, args = {}) {
    try {
      const result = await UI.post("/api/command", { action, args });
      message = { text: `${action}: ${result.accepted ? "accepted" : "refused"} — ${result.reason}`, ok: result.accepted };
      return result.accepted;
    } catch (error) {
      message = { text: `${action}: ${error.message}`, ok: false };
      if (error.status === 401) refreshSession();
      return false;
    } finally {
      showMessage();
    }
  }

  function showMessage() {
    for (const id of ["manual-message", "detail-manual-message", "op-message"]) {
      const el = document.getElementById(id);
      if (el) { el.textContent = message.text; el.className = message.ok ? "muted" : "note"; }
    }
  }

  async function request(cmd, value) {
    wanted[cmd] = { value, sent: Date.now() };
    if (!(await send("manual_request", { command: cmd, value }))) delete wanted[cmd];
  }

  async function release(cmd) {
    delete wanted[cmd];
    await send("manual_release", { command: cmd });
  }

  async function allOff() {
    for (const key of Object.keys(wanted)) delete wanted[key];
    for (const key of Object.keys(overrides)) delete overrides[key];
    valveTest = null;
    await send("manual_clear");
  }

  async function override(sw, value) {
    if (value === null) { delete overrides[sw]; return send("cancel_override", { switch: sw }); }
    overrides[sw] = { value, sent: Date.now() };
    if (!(await send("level_override", { switch: sw, value }))) delete overrides[sw];
  }

  async function toggleValveTest() {
    if (valveTest) { valveTest = null; message = { text: "Valve test allowance no longer renewed (expires within 30 s)", ok: true }; showMessage(); return; }
    valveTest = { sent: Date.now() };
    if (!(await send("allow_valve_test"))) valveTest = null;
  }

  /* Renew while wanted; drop everything when the selector leaves MANUAL. */
  async function tick() {
    const s = st();
    if (s && isLive() && s.mode !== "MANUAL") {
      for (const key of Object.keys(wanted)) delete wanted[key];
      for (const key of Object.keys(overrides)) delete overrides[key];
      valveTest = null;
      return;
    }
    if (blockers().length) return;
    const now = Date.now();
    for (const [cmd, w] of Object.entries(wanted)) {
      if (now - w.sent >= RENEW_MS) {
        w.sent = now;
        if (!(await send("manual_request", { command: cmd, value: w.value, renew: true }))) delete wanted[cmd];
      }
    }
    for (const [sw, o] of Object.entries(overrides)) {
      if (now - o.sent >= OVERRIDE_RENEW_MS) {
        o.sent = now;
        if (!(await send("level_override", { switch: sw, value: o.value, renew: true }))) delete overrides[sw];
      }
    }
    if (valveTest && now - valveTest.sent >= RENEW_MS) {
      valveTest.sent = now;
      if (!(await send("allow_valve_test", { renew: true }))) valveTest = null;
    }
  }

  /* Leaving the page: withdraw this user's requests at once (best effort;
     the controller's expiry is the fallback). */
  function withdrawOnLeave() {
    if (!Object.keys(wanted).length && !Object.keys(overrides).length && !valveTest) return;
    fetch("/api/command", {
      method: "POST", keepalive: true, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "mwt-gui" },
      body: JSON.stringify({ action: "manual_clear", args: { own: true, cause: "PAGE_CLOSED" } }),
    });
  }

  function forgetLocal() {
    for (const key of Object.keys(wanted)) delete wanted[key];
    for (const key of Object.keys(overrides)) delete overrides[key];
    valveTest = null;
  }

  function outputNote(s) {
    if (s.data_source === "SIMULATED") return ["shadow", "SIMULATION: commands switch the simulated plant, not the RevPi outputs."];
    if (s.outputs_enabled === false) return ["shadow", "Outputs DISABLED (shadow run): commands run through the logic but nothing is written."];
    return ["live", "Outputs ENABLED: accepted commands switch the RevPi outputs (relays)."];
  }

  /* Buttons for one output (manual tab and the detail panel). */
  function actionButtons(cmd, enabled) {
    const dis = enabled ? "" : "disabled";
    return cmd === DRAIN
      ? `<button data-on="${cmd}" data-value="0" class="danger" ${dis}>STOP (inhibit)</button>
         <button data-off="${cmd}" class="secondary" ${dis}>Automatic</button>`
      : cmd === "control_valve_percent"
      ? `<input type="number" min="0" max="100" step="1" value="0" data-y4-input ${dis}> %
         <button data-set="${cmd}" ${dis}>Set</button>
         <button data-off="${cmd}" class="secondary" ${dis}>Close (release)</button>`
      : `<button data-on="${cmd}" ${dis}>${isValve(cmd) ? "Open" : "ON"}</button>
         <button data-off="${cmd}" class="secondary" ${dis}>${isValve(cmd) ? "Close" : "OFF"}</button>`;
  }

  function overrideButtons(sw, enabled) {
    const dis = enabled ? "" : "disabled";
    return `<button data-ov="${sw}" data-val="1" class="secondary" ${dis}>Force reached</button>
      <button data-ov="${sw}" data-val="0" class="secondary" ${dis}>Force not reached</button>
      <button data-ov="${sw}" data-val="" class="secondary" ${dis}>Cancel</button>`;
  }

  /* Wire the buttons inside one container only. */
  function bind(root) {
    for (const b of $$("[data-on]", root)) b.onclick = () => request(b.dataset.on, b.dataset.value !== "0");
    for (const b of $$("[data-off]", root)) b.onclick = () => release(b.dataset.off);
    for (const b of $$("[data-set]", root)) b.onclick = () => {
      const value = Number($("[data-y4-input]", root).value);
      if (Number.isFinite(value) && value >= 0 && value <= 100) request(b.dataset.set, value);
      else { message = { text: "Y4 position must be 0-100 %", ok: false }; showMessage(); }
    };
    for (const b of $$("[data-ov]", root)) b.onclick = () => override(b.dataset.ov, b.dataset.val === "" ? null : b.dataset.val === "1");
  }

  function build(enabled) {
    const c = cfg(), s = st();
    const commands = c.manual_commands || [];
    const pending = new Set(c.pending_identification || []);
    const rows = commands.map((cmd) => {
      const t = tag(cmd), channel = (c.output_channels || {})[cmd] || "—";
      const actions = actionButtons(cmd, enabled);
      return `<tr data-row="${cmd}"><td><b>${UI.esc(t.tag)}</b><br><span class="muted small">${UI.esc(t.name)}</span></td>
        <td class="nowrap">${UI.esc(channel)}${pending.has(channel) ? `<br><span class="note small">identity pending</span>` : ""}</td>
        <td data-cell="wanted"></td><td data-cell="commanded"></td><td data-cell="written"></td>
        <td data-cell="note" class="small"></td><td class="nowrap">${actions}</td></tr>`;
    }).join("");
    const roles = c.level_roles || {};
    const levels = Object.entries(roles).map(([role, sw]) => `<tr data-level="${sw}">
        <td><b>${UI.esc(tag(sw).tag)}</b><br><span class="muted small">${UI.esc(role.replace(/_/g, " "))}</span></td>
        <td data-cell="real"></td><td data-cell="effective"></td>
        <td class="nowrap">${overrideButtons(sw, enabled)}</td></tr>`).join("");
    const [noteClass, noteText] = s ? outputNote(s) : ["held", "No controller data."];
    const reasons = blockers();
    $("#view-manual").innerHTML = `
      <div class="manual-head">
        <h2>Manual mode</h2>
        <p class="output-note output-${noteClass}">${UI.esc(noteText)}</p>
        ${reasons.length ? `<ul class="blockers">${reasons.map((r) => `<li>${UI.esc(r)}</li>`).join("")}</ul>` : ""}
        <p class="muted">Requests expire ${c.request_expiry_s || 30} s after the last renewal; this page renews them while it is open.
          Interlocks stay active: "Commanded" is what the controller allows, "Written" what reached the outputs.</p>
        <div class="manual-buttons">
          <button id="manual-all-off" class="danger" ${SESSION.user ? "" : "disabled"}>All manual requests OFF</button>
          <button id="manual-valve-test" class="secondary" ${enabled ? "" : "disabled"}>${valveTest ? "Stop renewing Y3/Y4 test allowance" : "Allow Y3/Y4 test during pasteurizer Level 3"}</button>
        </div>
        <p id="manual-message"></p>
      </div>
      <table class="manual"><thead><tr><th>Equipment</th><th>RevPi output</th><th>Your request</th><th>Commanded</th>
        <th>Written</th><th>Interlock / alarm</th><th>Command</th></tr></thead><tbody>${rows}</tbody></table>
      <h3>Level switch overrides (${c.override_expiry_s || 10} s, renewed while active)</h3>
      <p class="muted small">A real protective reading always wins: a reached high or overflow switch, a not-reached low switch.</p>
      <table class="manual"><thead><tr><th>Switch</th><th>Real reading</th><th>Used by the logic</th><th>Override</th></tr></thead>
        <tbody>${levels}</tbody></table>`;
    bind($("#view-manual"));
    $("#manual-all-off").onclick = allOff;
    $("#manual-valve-test").onclick = async () => { await toggleValveTest(); structure = ""; render(); };
    showMessage();
  }

  function update() {
    const s = st();
    if (!s) return;
    for (const row of $$("[data-row]")) {
      const cmd = row.dataset.row;
      const w = wanted[cmd];
      $("[data-cell=wanted]", row).textContent = w ? wantedText(cmd, w.value) : "—";
      $("[data-cell=commanded]", row).textContent = fmt(cmd, s.commanded[cmd]);
      $("[data-cell=written]", row).textContent = s.outputs_enabled === false ? "not written (outputs disabled)"
        : s.written ? fmt(cmd, s.written[cmd]) : "not written";
      const notes = (s.arbitration_notes || []).filter((n) => n.output === cmd).map((n) => n.message);
      const alarms = (s.alarms || []).filter((a) => (a.equipment || []).includes(cmd)).map((a) => a.code);
      $("[data-cell=note]", row).textContent = [...notes, ...alarms].join(" · ");
      row.classList.toggle("row-active", !!w);
    }
    const levels = (s.signals && s.signals.levels) || {};
    for (const row of $$("[data-level]")) {
      const sw = row.dataset.level, lv = levels[sw];
      const actual = (s.override_actual || {})[sw];
      const real = sw in (s.override_actual || {}) ? actual : lv && lv.valid ? lv.reached : null;
      const word2 = (v) => (v === true ? "Reached" : v === false ? "Not reached" : "Unknown");
      $("[data-cell=real]", row).textContent = word2(real);
      $("[data-cell=effective]", row).textContent = word2(lv && lv.valid ? lv.reached : null) +
        ((s.overrides || []).includes(sw) ? " (override)" : "");
      row.classList.toggle("row-active", !!overrides[sw]);
    }
  }

  /* Detail panel: manual buttons for the clicked part. Shown only while the
     controller reports MANUAL with current data; the same checks as the tab apply. */
  let detailKey = "";
  function renderDetail(id) {
    const box = document.getElementById("card-manual");
    if (!box) return;
    const s = st(), c = cfg();
    const isCmd = !!id && (c.manual_commands || []).includes(id);
    const isSwitch = !!id && Object.values(c.level_roles || {}).includes(id);
    if (!id || !s || !isLive() || s.mode !== "MANUAL" || (!isCmd && !isSwitch)) {
      box.hidden = true; box.innerHTML = ""; detailKey = ""; return;
    }
    box.hidden = false;
    const reasons = blockers(), enabled = reasons.length === 0;
    const key = JSON.stringify([id, enabled, reasons, s.outputs_enabled, s.data_source]);
    if (key !== detailKey) {
      detailKey = key;
      const [noteClass, noteText] = outputNote(s);
      box.innerHTML = `<h3>Manual control</h3>
        <p class="output-note output-${noteClass}">${UI.esc(noteText)}</p>
        ${reasons.length ? `<ul class="blockers">${reasons.map((r) => `<li>${UI.esc(r)}</li>`).join("")}</ul>` : ""}
        ${isCmd ? `<table class="kv"><tr><th>Your request</th><td data-dm="wanted"></td></tr>
            <tr><th>Commanded</th><td data-dm="commanded"></td></tr>
            <tr><th>Interlock / alarm</th><td data-dm="note"></td></tr></table>
          <div class="detail-actions">${actionButtons(id, enabled)}</div>`
          : `<table class="kv"><tr><th>Real reading</th><td data-dm="real"></td></tr>
            <tr><th>Used by the logic</th><td data-dm="effective"></td></tr></table>
          <div class="detail-actions">${overrideButtons(id, enabled)}</div>`}
        <p class="muted small">Requests are renewed while this page is open. The manual tab shows all outputs.</p>
        <p id="detail-manual-message"></p>`;
      bind(box);
      showMessage();
    }
    const cell = (k) => $(`[data-dm="${k}"]`, box);
    if (isCmd) {
      const w = wanted[id];
      cell("wanted").textContent = w ? wantedText(id, w.value) : "—";
      cell("commanded").textContent = fmt(id, s.commanded[id]);
      const notes = (s.arbitration_notes || []).filter((n) => n.output === id).map((n) => n.message);
      const alarms = (s.alarms || []).filter((a) => (a.equipment || []).includes(id)).map((a) => a.code);
      cell("note").textContent = [...notes, ...alarms].join(" · ") || "—";
    } else {
      const lv = ((s.signals && s.signals.levels) || {})[id];
      const real = id in (s.override_actual || {}) ? s.override_actual[id] : lv && lv.valid ? lv.reached : null;
      const word2 = (v) => (v === true ? "Reached" : v === false ? "Not reached" : "Unknown");
      cell("real").textContent = word2(real);
      cell("effective").textContent = word2(lv && lv.valid ? lv.reached : null) + ((s.overrides || []).includes(id) ? " (override)" : "");
    }
  }

  function render() {
    if (APP.selected) renderDetail(APP.selected);
    if (APP.tab !== "manual") return;
    const s = st();
    const enabled = blockers().length === 0;
    const key = JSON.stringify([enabled, SESSION.user, SESSION.role, s && s.outputs_enabled,
      s && s.mode, isLive(), (cfg().manual_commands || []).length, !!valveTest]);
    if (key !== structure) { structure = key; build(enabled); }
    update();
  }

  /* Session ----------------------------------------------------------------- */
  async function refreshSession() {
    try {
      Object.assign(SESSION, await UI.get("/api/session"));
    } catch (error) { SESSION.user = SESSION.role = null; }
    if (!SESSION.user) forgetLocal();     // logged out or expired: nothing is renewed
    $("#session").textContent = SESSION.user ? `${SESSION.user} · ${SESSION.role}`
      : SESSION.logins ? "Read-only (not logged in)" : "Read-only (no logins configured)";
    $("#login-btn").hidden = !!SESSION.user || !SESSION.logins;
    $("#logout-btn").hidden = !SESSION.user;
    $("#operator-actions").hidden = !SESSION.user || !SESSION.commands;
    structure = "";
    render();
  }

  function init() {
    $("#login-btn").onclick = () => { $("#login-dialog").hidden = false; $("#login-error").textContent = ""; $("#login-name").focus(); };
    $("#login-cancel").onclick = () => { $("#login-dialog").hidden = true; };
    $("#login-form").onsubmit = async (ev) => {
      ev.preventDefault();
      try {
        await UI.post("/api/login", { name: $("#login-name").value, password: $("#login-password").value });
        $("#login-password").value = "";
        $("#login-dialog").hidden = true;
        await refreshSession();
      } catch (error) {
        $("#login-error").textContent = error.message;
      }
    };
    $("#logout-btn").onclick = async () => {
      forgetLocal();                // the GUI server withdraws this user's requests at logout
      try { await UI.post("/api/logout"); } catch (error) { /* already gone */ }
      await refreshSession();
    };
    for (const b of $$("[data-op]")) b.onclick = () => send(b.dataset.op);
    window.addEventListener("pagehide", withdrawOnLeave);
    refreshSession();
    setInterval(refreshSession, 30000);
    setInterval(tick, 1000);
  }

  return { init, render, renderDetail, send, refreshSession };
})();
