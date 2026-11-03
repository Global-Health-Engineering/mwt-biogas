/* Shared helpers for the main window and the log window. */
"use strict";

const UI = {
  async get(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).error || detail; } catch (e) { /* not JSON */ }
      throw new Error(detail);
    }
    return response.json();
  },

  esc(text) {
    return String(text ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  },

  tzLabel(date = new Date()) {
    const minutes = -date.getTimezoneOffset();
    const sign = minutes >= 0 ? "+" : "-";
    const abs = Math.abs(minutes);
    return `UTC${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
  },

  /* Local time with its offset, e.g. "2026-11-03 10:25:04 UTC+01:00". */
  time(iso, withDate = true) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const pad = (n) => String(n).padStart(2, "0");
    const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    return `${withDate ? day + " " : ""}${clock} ${UI.tzLabel(d)}`;
  },

  clockQuality(source, sync) {
    if (source === "SIMULATED") return "Simulated time";
    if (sync === "SYNCHRONISED") return "Clock synchronised";
    if (sync === "UNSYNCHRONISED") return "Clock NOT synchronised";
    return "Clock sync unknown";
  },

  severity(sev) {
    const map = {
      CRITICAL: ["▲", "Critical", "sev-critical"],
      WARNING: ["!", "Warning", "sev-warning"],
      INFO: ["i", "Info", "sev-info"],
    };
    const [icon, text, cls] = map[sev] || map.INFO;
    return `<span class="sev ${cls}"><b class="sev-icon">${icon}</b>${text}</span>`;
  },

  tagOf(meta, id) {
    const e = meta && meta.byId[id];
    return e ? e.tag : id;
  },

  indexMeta(meta) {
    meta.byId = {};
    for (const e of meta.equipment) meta.byId[e.id] = e;
    return meta;
  },
};
