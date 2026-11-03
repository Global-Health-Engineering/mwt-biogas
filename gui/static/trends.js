/* Trends tab: temperatures, flows and Y4, one panel per unit. Lines break at
   invalid readings, controller restarts and logged gaps. */
"use strict";

const TRENDS = (() => {
  const SERIES = [
    ["PT1", "PT1 °C"], ["PT2", "PT2 °C"], ["PT3", "PT3 °C"], ["PT4", "PT4 °C"], ["PT5", "PT5 °C"], ["PT6", "PT6 °C"],
    ["FLOW1", "EDB-FR-01 L/min"], ["FLOW2", "EDB-FR-02 L/min"], ["FLOW3", "PST-FI-01 L/min"],
    ["Y4_REQUESTED", "Y4 requested %"], ["Y4_WRITTEN", "Y4 written %"], ["Y4_FEEDBACK", "Y4 measured %"],
  ];
  const COLOURS = ["#6cb6ff", "#f69d50", "#e5534b", "#8ddb8c", "#dcbdfb", "#e0c55c", "#96d0ff", "#ffb3d9", "#7ee2b8", "#c6cdd5", "#f9a03f", "#56d4dd"];
  const chosen = new Set(["PT3", "PT5", "PT6", "FLOW3", "Y4_REQUESTED", "Y4_FEEDBACK"]);
  let timer = null;

  function init() {
    const box = document.getElementById("trend-series");
    box.innerHTML = SERIES.map(([id, text], i) => `<label><input type="checkbox" value="${id}" ${chosen.has(id) ? "checked" : ""}>
      <i style="background:${COLOURS[i]}"></i>${text}</label>`).join("");
    box.onchange = (ev) => { ev.target.checked ? chosen.add(ev.target.value) : chosen.delete(ev.target.value); load(); };
    document.getElementById("trend-range").onchange = load;
    document.getElementById("trend-fit").onchange = load;
    document.getElementById("trend-refresh").onclick = load;
  }

  function show() {
    load();
    clearInterval(timer);
    timer = setInterval(() => { if (!document.getElementById("view-trends").hidden) load(); }, 15000);
  }

  async function load() {
    const minutes = Number(document.getElementById("trend-range").value);
    const st = APP.view && APP.view.status;
    const until = new Date(st && st.utc ? new Date(st.utc).getTime() + 1000 : Date.now());
    const since = new Date(until.getTime() - minutes * 60000);
    const names = SERIES.map(([id]) => id).filter((id) => chosen.has(id));
    const out = document.getElementById("trend-panels");
    if (!names.length) { out.innerHTML = `<p class="muted">Choose at least one series.</p>`; return; }
    try {
      const data = await UI.get(`/api/trend?series=${names.join(",")}&since=${since.toISOString()}&until=${until.toISOString()}&max_points=800`);
      draw(data, since, until);
    } catch (error) {
      out.innerHTML = `<p class="note">Trend unavailable: ${UI.esc(error.message)}</p>`;
    }
  }

  function draw(data, since, until) {
    const units = {};
    for (const [name, s] of Object.entries(data.series)) (units[s.units] = units[s.units] || []).push(name);
    const sim = data.sources.some(([src, clock]) => src === "SIMULATED" || clock === "SIMULATED");
    // "Fit available data": the time axis spans the first to the last sample
    // in the chosen range, so a short run fills the width.
    const times = Object.values(data.series).flatMap((s) => s.points.map((p) => p.t));
    const fit = document.getElementById("trend-fit").checked && times.length > 1;
    const t0 = fit ? Math.min(...times) : since.getTime() / 1000;
    const t1 = fit ? Math.max(...times) : until.getTime() / 1000;
    if (fit) { since = new Date(t0 * 1000); until = new Date(t1 * 1000); }
    const panelWidth = document.getElementById("trend-panels").clientWidth;
    const W = Math.max(600, Math.round(panelWidth || 1000)), H = 230, L = 56, R = 20, T = 22, B = 26;
    const X = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
    let html = `<p class="muted">${UI.time(since.toISOString())} to ${UI.time(until.toISOString())} · ${fit ? "(fitted to the available data) · " : ""}${data.samples_in_range} samples${data.stride > 1 ? `, every ${data.stride}th shown` : ""}${sim ? " · <b>SIMULATED data and timestamps</b>" : ""}</p>`;
    for (const [unit, names] of Object.entries(units)) {
      const values = names.flatMap((n) => data.series[n].points.map((p) => p.value)).filter((v) => v != null);
      let lo = values.length ? Math.min(...values) : 0, hi = values.length ? Math.max(...values) : 1;
      if (hi - lo < 1) { lo -= 0.5; hi += 0.5; }
      const allPositive = values.every((v) => v >= 0);
      const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      if (allPositive) lo = Math.max(lo, 0);   // flows and % never show a negative axis
      const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
      let svg = `<svg viewBox="0 0 ${W} ${H}" class="trend"><rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" class="plot"/>`;
      for (let i = 0; i <= 4; i++) {
        const v = lo + ((hi - lo) * i) / 4;
        svg += `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" class="grid"/><text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end" class="axis">${v.toFixed(1)}</text>`;
      }
      for (let i = 0; i <= 4; i++) {
        const t = t0 + ((t1 - t0) * i) / 4;
        svg += `<text x="${X(t)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}" class="axis">${UI.time(new Date(t * 1000).toISOString(), false).slice(0, 8)}</text>`;
      }
      for (const gap of data.gaps) {
        const t = new Date(gap.utc).getTime() / 1000;
        svg += `<line x1="${X(t)}" x2="${X(t)}" y1="${T}" y2="${H - B}" class="gap"><title>Historian gap: ${UI.esc(gap.data.cause)}</title></line>`;
      }
      for (const name of names) {
        const colour = COLOURS[SERIES.findIndex(([id]) => id === name)];
        let path = "", pen = false, run = null;
        for (const p of data.series[name].points) {
          if (p.value == null || p.run !== run) pen = false;   // break the line
          run = p.run;
          if (p.value == null) continue;
          path += `${pen ? "L" : "M"}${X(p.t).toFixed(1)} ${Y(p.value).toFixed(1)}`;
          pen = true;
        }
        svg += `<path d="${path}" stroke="${colour}" class="line"/>`;
      }
      svg += `<text x="${L - 6}" y="${T - 8}" text-anchor="end" class="axis unit">${UI.esc(unit === "degC" ? "°C" : unit)}</text></svg>`;
      html += `<div class="trend-panel"><div class="trend-title">${names.map((n) => SERIES.find(([id]) => id === n)[1]).join(" · ")}</div>${svg}</div>`;
    }
    html += `<p class="muted">Lines break at invalid readings and controller restarts; dashed lines mark historian gaps.</p>`;
    document.getElementById("trend-panels").innerHTML = html;
  }

  return { init, show };
})();
