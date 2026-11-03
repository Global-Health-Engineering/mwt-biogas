/* Log window: filters, pages of 100, live follow that pauses when you scroll
   back, CSV export of the current filter. Uses the same GUI server. */
"use strict";

const LOG = { meta: null, rows: [], newest: null, olderCursor: null, live: true, pageCursors: [] };
const KINDS = ["STATE_TRANSITION", "MODE_CHANGE", "ALARM_RAISED", "ALARM_CLEARED", "ALARM_RESET",
  "ALARM_ACKNOWLEDGED", "OPERATOR_ACTION", "OUTPUT_REQUESTED", "OUTPUT_WRITTEN", "OUTPUT_WRITE_FAILED",
  "OUTPUT_WRITE_RECOVERED", "MODEL_STATUS", "LOG_GAP", "CLOCK_CORRECTION", "CLOCK_SYNC_CHANGED",
  "CONTROLLER_START", "CONTROLLER_STOP", "CONFIG_CHANGED", "PREVIOUS_RUN_UNCLEAN",
  "HISTORIAN_DB_RECOVERED", "HISTORIAN_STORAGE", "MANUAL_EXPIRED", "INDICATORS"];
const $ = (s) => document.querySelector(s);

function filterQuery() {
  const q = new URLSearchParams();
  const iso = (v) => (v ? new Date(v).toISOString() : "");
  if ($("#f-since").value) q.set("since", iso($("#f-since").value));
  if ($("#f-until").value) q.set("until", iso($("#f-until").value));
  if ($("#f-severity").value) q.set("severity", $("#f-severity").value);
  if ($("#f-equipment").value) q.set("equipment", $("#f-equipment").value);
  if ($("#f-kind").value) q.set("kinds", $("#f-kind").value);
  return q;
}

function rowHtml(r) {
  return `<tr class="${r.severity === "CRITICAL" ? "row-critical" : r.severity === "WARNING" ? "row-warning" : ""}">
    <td class="nowrap">${UI.time(r.utc)}</td>
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
    $("#source").textContent = src === "SIMULATED" ? "SIMULATION" : src === "LIVE" ? "LIVE" : "SOURCE UNKNOWN";
    $("#source").className = "badge " + (src === "SIMULATED" ? "badge-sim" : src === "LIVE" ? "badge-live" : "badge-unknown");
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
