/* Log window: filters, pages of 100, live follow that pauses when you scroll
   back, CSV export of the current filter. Uses the same GUI server.
   Events of one scan cycle share data.cycle: clicking a row highlights the
   others (e.g. a level change and the pump request it led to). */
"use strict";

const LOG = { meta: null, rows: [], newest: null, olderCursor: null, live: true, pageCursors: [],
  selected: null };
/* What the inputs did (signal_watch.py); hidden in the operator view. */
const SIGNAL_KINDS = ["LEVEL_CHANGED", "SENSOR_VALIDITY", "FLOW_STARTED", "FLOW_STOPPED"];
const KINDS = ["STATE_TRANSITION", "MODE_CHANGE", ...SIGNAL_KINDS, "ALARM_RAISED", "ALARM_CLEARED",
  "ALARM_RESET", "ALARM_ACKNOWLEDGED", "OPERATOR_ACTION", "MANUAL_EXPIRED", "MANUAL_RELEASE_AUTOMATIC",
  "OUTPUT_REQUESTED", "OUTPUT_WRITTEN", "OUTPUT_NOT_WRITTEN", "OUTPUT_WRITE_FAILED",
  "OUTPUT_WRITE_RECOVERED", "WRITE_EXCEPTION", "INDICATORS", "MODEL_STATUS", "MODEL_RESULT_REJECTED",
  "LOW_FLOW_RETRY_RESTORED", "CONTROLLER_START", "CONTROLLER_STOP", "CONFIG_CHANGED",
  "PREVIOUS_RUN_UNCLEAN", "STATE_SAVE_FAILED", "STATE_SAVE_RECOVERED", "FAULT_STORAGE_UNREADABLE",
  "LOG_GAP", "CLOCK_CORRECTION", "CLOCK_SYNC_CHANGED", "HISTORIAN_DB_RECOVERED",
  "HISTORIAN_RECOVERY_ACKNOWLEDGED", "HISTORIAN_STORAGE", "SIGNAL_WATCH_ERROR", "OUTPUT_GATE"];
const $ = (s) => document.querySelector(s);

function filterQuery() {
  const q = new URLSearchParams();
  const iso = (v) => (v ? new Date(v).toISOString() : "");
  if ($("#f-since").value) q.set("since", iso($("#f-since").value));
  if ($("#f-until").value) q.set("until", iso($("#f-until").value));
  if ($("#f-severity").value) q.set("severity", $("#f-severity").value);
  if ($("#f-equipment").value) q.set("equipment", $("#f-equipment").value);
  if ($("#f-kind").value) q.set("kinds", $("#f-kind").value);
  else if ($("#f-detail").value === "operator") q.set("exclude_kinds", SIGNAL_KINDS.join(","));
  return q;
}

/* Events of one scan cycle of one controller run. */
function cycleKey(r) {
  const cycle = r.data && r.data.cycle;
  return cycle === undefined || cycle === null ? null : `${r.boot_id}:${cycle}`;
}

function rowClass(r) {
  const classes = [r.severity === "CRITICAL" ? "row-critical" : r.severity === "WARNING" ? "row-warning"
    : r.kind === "OUTPUT_NOT_WRITTEN" ? "row-notwritten" : ""];
  if (SIGNAL_KINDS.includes(r.kind)) classes.push("row-signal");
  const key = cycleKey(r);
  if (key) classes.push("row-linkable");
  if (key && key === LOG.selected) classes.push("row-related");
  return classes.filter(Boolean).join(" ");
}

function rowHtml(r) {
  const key = cycleKey(r);
  return `<tr class="${rowClass(r)}"${key ? ` data-cycle="${UI.esc(key)}"` : ""}>
    <td class="nowrap">${UI.time(r.utc)}${key ? `<br><span class="muted small">scan ${UI.esc(r.data.cycle)}</span>` : ""}</td>
    <td><span class="src">${UI.esc(r.source)}</span><br><span class="muted small">${UI.esc(UI.clockQuality(r.clock_source, r.clock_sync))}</span></td>
    <td>${UI.severity(r.severity)}</td>
    <td>${r.equipment.map((id) => `<span class="nowrap">${UI.esc(UI.tagOf(LOG.meta, id))}</span>`).join(", ")}</td>
    <td class="kind">${UI.esc(r.kind)}</td><td class="desc">${UI.esc(r.description)}</td>
    <td class="muted">${UI.esc(r.procedure || "")}</td></tr>`;
}

function render() {
  $("#log-rows").innerHTML = LOG.rows.map(rowHtml).join("") ||
    `<tr><td colspan="7" class="muted">No events for this filter.</td></tr>`;
  $("#live-state").textContent = LOG.live ? "Live follow on" : "Live follow paused";
  $("#live-state").className = "chip " + (LOG.live ? "chip-ok" : "chip-warn");
  $("#resume").hidden = LOG.live;
  $("#older").disabled = LOG.olderCursor === null;
  $("#page-info").textContent = `${LOG.rows.length} events shown${LOG.pageCursors.length ? ` · page ${LOG.pageCursors.length + 1}` : ""}`;
  const related = LOG.selected ? LOG.rows.filter((r) => cycleKey(r) === LOG.selected).length : 0;
  $("#related-info").textContent = LOG.selected
    ? `${related} event${related === 1 ? "" : "s"} of scan ${LOG.selected.split(":")[1]} highlighted · click again to clear`
    : "Click an event to highlight everything from the same scan cycle.";
  $("#export").href = "/api/export.csv?" + filterQuery().toString();
}

async function loadPage(beforeId = null) {
  const q = filterQuery();
  q.set("limit", "100");
  if (beforeId !== null) q.set("before_id", beforeId);
  try {
    const page = await UI.get("/api/events?" + q.toString());
    LOG.rows = page.rows;
    LOG.olderCursor = page.next_before_id;
    if (beforeId === null) LOG.newest = page.newest_id;
  } catch (error) {
    LOG.rows = [];
    $("#log-rows").innerHTML = `<tr><td colspan="7" class="note">History unavailable: ${UI.esc(error.message)}</td></tr>`;
    return;
  }
  render();
}

async function follow() {
  if (!LOG.live || LOG.newest === null || LOG.pageCursors.length) return;
  const q = filterQuery();
  q.set("after_id", LOG.newest);
  q.set("limit", "100");
  try {
    const page = await UI.get("/api/events?" + q.toString());
    if (page.rows.length) {
      LOG.rows = page.rows.concat(LOG.rows).slice(0, 500);
      render();
    }
    LOG.newest = page.newest_id;
  } catch (error) { /* shown by the header */ }
}

async function header() {
  try {
    const v = await UI.get("/api/live");
    const src = v.status ? v.status.data_source : "UNKNOWN";
    const shadow = v.status && v.status.outputs_enabled === false;
    $("#source").textContent = shadow ? "LIVE INPUTS — OUTPUTS DISABLED / SHADOW RUN" : src === "SIMULATED" ? "SIMULATION" : src === "LIVE" ? "LIVE — OUTPUTS ENABLED" : "SOURCE UNKNOWN";
    $("#source").className = "badge " + (shadow ? "badge-shadow" : src === "SIMULATED" ? "badge-sim" : src === "LIVE" ? "badge-outputs" : "badge-unknown");
    $("#conn").textContent = v.state === "CONNECTED" ? "Controller connected" : `Controller ${v.state.toLowerCase()}`;
    $("#conn").className = "conn conn-" + v.state.toLowerCase();
  } catch (error) {
    $("#conn").textContent = "GUI server unreachable";
    $("#conn").className = "conn conn-disconnected";
  }
}

function pause() { if (LOG.live) { LOG.live = false; render(); } }

async function resume() {
  LOG.live = true;
  LOG.pageCursors = [];
  $("#log-scroll").scrollTop = 0;
  await loadPage();
}

async function init() {
  $("#tz").textContent = `local time (${UI.tzLabel()})`;
  LOG.meta = UI.indexMeta(await UI.get("/api/meta"));
  for (const e of LOG.meta.equipment.filter((x) => !["manual", "static"].includes(x.kind))) {
    $("#f-equipment").insertAdjacentHTML("beforeend", `<option value="${e.id}">${UI.esc(e.tag)} · ${UI.esc(e.name)}</option>`);
  }
  for (const k of KINDS) $("#f-kind").insertAdjacentHTML("beforeend", `<option>${k}</option>`);
  $("#filters").onsubmit = (ev) => { ev.preventDefault(); LOG.pageCursors = []; loadPage(); };
  $("#f-clear").onclick = () => { $("#filters").reset(); LOG.pageCursors = []; loadPage(); };
  $("#f-detail").onchange = () => { LOG.pageCursors = []; loadPage(); };
  $("#log-rows").addEventListener("click", (ev) => {
    const row = ev.target.closest("tr[data-cycle]");
    if (!row) return;
    LOG.selected = LOG.selected === row.dataset.cycle ? null : row.dataset.cycle;
    render();
  });
  $("#older").onclick = () => { pause(); LOG.pageCursors.push(LOG.olderCursor); loadPage(LOG.olderCursor); };
  $("#newest").onclick = resume;
  $("#resume").onclick = resume;
  $("#log-scroll").addEventListener("scroll", () => { if ($("#log-scroll").scrollTop > 40) pause(); });
  await header();
  await loadPage();
  setInterval(header, 2000);
  setInterval(follow, 2000);
}

init();
