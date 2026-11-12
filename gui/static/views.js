/* Process drawings (SVG) after GUI_V2: arrangement and tags kept.
   Dynamic parts carry data-id (software id) and get values from app.js.

   Label rules (both views):
   - inline parts on horizontal pipes: tag above the pipe
   - parts on vertical pipes: tag to the right
   - sensors off the pipe: label on the side away from the pipe
   - pumps and motor valves: command lamp and text above the part,
     tag and name below (pumps) or beside (valves) */
"use strict";

const VIEWS = (() => {
  const C = {
    effluent: "#B20000", water: "#2f7fd1",                 // EDB view
    filterEff: "#b46504", hot: "#FF0000", past: "#2D7600", biogas: "#B09500",
  };
  const W = 1400, H_VIEW = 760;
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const pipe = (pts, colour, cls = "") =>
    `<polyline class="pipe ${cls}" points="${pts.map((p) => p.join(",")).join(" ")}" stroke="${colour}"/>`;
  const jump = (x, y, colour) =>   // a pipe crossing another without connection
    `<path class="pipe" d="M${x} ${y - 8} a8 8 0 0 1 0 16" stroke="${colour}"/>`;
  const label = (x, y, text, cls = "tag", anchor = "middle") =>
    `<text class="${cls}" x="${x}" y="${y}" text-anchor="${anchor}">${esc(text)}</text>`;
  const arrow = (x, y, w, text, fill, dir = 1) => {
    const h = 34, tip = 24 * dir, x2 = x + w * dir;
    return `<g class="terminal"><polygon points="${x},${y - h / 2 + 6} ${x2},${y - h / 2 + 6} ${x2},${y - h / 2} ${x2 + tip},${y} ${x2},${y + h / 2} ${x2},${y + h / 2 - 6} ${x},${y + h / 2 - 6}" fill="${fill}"/>${label(x + (w * dir) / 2, y + 5, text, "terminal-text")}</g>`;
  };
  const outlet = (x, y, colour) =>   // small open pipe end
    `<polygon class="terminal" points="${x},${y - 8} ${x + 16},${y} ${x},${y + 8}" fill="${colour}"/>`;
  // Equipment groups: clickable, lamp + state text filled in by app.js.
  const lamp = (id, x, y) => `<circle class="lamp unknown" data-lamp="${id}" cx="${x}" cy="${y}" r="9"/>`;
  const state = (id, x, y, anchor = "start") =>
    `<text class="state" data-state="${id}" x="${x}" y="${y}" text-anchor="${anchor}">—</text>`;
  const req = (id, x, y, anchor = "start") =>
    `<text class="detail" data-req="${id}" x="${x}" y="${y}" text-anchor="${anchor}"></text>`;
  const detail = (id, x, y, anchor = "start") =>
    `<text class="detail" data-detail="${id}" x="${x}" y="${y}" text-anchor="${anchor}"></text>`;
  const group = (id, inner, title) =>
    `<g class="equip" data-id="${id}" tabindex="0"><title>${esc(title)}</title>${inner}</g>`;
  // Command lamp and text, centred over a part at (x, y).
  const command = (id, x, y) => lamp(id, x - 34, y) + state(id, x - 20, y + 5) + req(id, x - 20, y + 21);

  // Pump: command above, tag and name below.
  const pump = (id, x, y, tag, name) => group(id,
    `<circle class="sym" cx="${x}" cy="${y}" r="16"/><path class="sym" d="M${x} ${y - 16}h22v10h-6"/>` +
    `<path class="sym" d="M${x - 10} ${y + 12}l-6 10h32l-6 -10"/>` +
    command(id, x, y - 58) +
    label(x, y + 42, tag) + label(x, y + 58, name, "name"), `${tag} · ${name}`);

  const bowtie = (x, y, vertical) => vertical
    ? `<path class="sym valve" d="M${x - 10} ${y - 14}h20l-20 28h20z"/>`
    : `<path class="sym valve" d="M${x - 14} ${y - 10}v20l28 -20v20z"/>`;

  // Manual valve: vertical pipe -> tag right (left where blocked); horizontal pipe -> tag above.
  const manual = (x, y, tag, vertical, name = "Manual valve — position not monitored", side = "right") => group(`manual_${tag}`,
    bowtie(x, y, vertical) + (vertical ? (side === "left" ? label(x - 18, y + 5, tag, "tag manual-tag", "end")
                                                          : label(x + 18, y + 5, tag, "tag manual-tag", "start"))
                                       : label(x, y - 20, tag, "tag manual-tag")),
    `${tag} · ${name}`);

  // Motor valve on a vertical pipe: actuator right, command above the actuator,
  // tag and name right of the actuator.
  const motorValve = (id, x, y, tag, name) => {
    const ax = x + 36;
    return group(id,
      bowtie(x, y, true) + `<line class="sym" x1="${x + 10}" y1="${y}" x2="${ax - 14}" y2="${y}"/>` +
      `<circle class="sym" cx="${ax}" cy="${y}" r="14"/><text class="sym-text" x="${ax}" y="${y + 5}" text-anchor="middle">M</text>` +
      lamp(id, ax, y - 30) + state(id, ax + 14, y - 25) +
      label(ax + 22, y - 2, tag, "tag", "start") + label(ax + 22, y + 14, name, "name", "start") +
      req(id, ax + 22, y + 30),
      `${tag} · ${name} (command only, no position feedback)`);
  };

  // Level switch: label on the side away from its pipe.
  const levelSwitch = (id, x, y, type, tag, name, stubTo, side) => {
    const lx = side === "left" ? x - 26 : x + 26, anchor = side === "left" ? "end" : "start";
    return group(id,
      (stubTo === x ? "" : `<line class="sym" x1="${x + (stubTo > x ? 18 : -18)}" y1="${y}" x2="${stubTo}" y2="${y}"/>`) +
      `<circle class="sym switch" data-switch="${id}" cx="${x}" cy="${y}" r="18"/>` +
      `<text class="sym-text small" x="${x}" y="${y + 4}" text-anchor="middle">${type}</text>` +
      label(lx, y - 2, tag, "tag", anchor) + state(id, lx, y + 15, anchor),
      `${tag} · ${name}`);
  };

  const flowMeter = (id, x, y, tag, name, toX, side) => {   // side: "left" | "right"
    const lx = side === "left" ? x - 26 : x + 26, ly = y - 6;
    const anchor = side === "left" ? "end" : "start";
    return group(id,
      (typeof toX === "object"    // {y}: vertical connection to a pipe above or below
        ? `<line class="sym" x1="${x}" y1="${y + (toX.y > y ? 18 : -18)}" x2="${x}" y2="${toX.y}"/>`
        : `<line class="sym" x1="${x + (toX > x ? 18 : -18)}" y1="${y}" x2="${toX}" y2="${y}"/>`) +
      `<circle class="sym" cx="${x}" cy="${y}" r="18"/><text class="sym-text small" x="${x}" y="${y + 4}" text-anchor="middle">l/min</text>` +
      label(lx, ly, tag, "tag", anchor) +
      `<text class="value" data-value="${id}" x="${lx}" y="${ly + 20}" text-anchor="${anchor}">—</text>` +
      detail(id, lx, ly + 36, anchor),
      `${tag} · ${name}`);
  };

  const filter = (x, y, tag) => group(`filter_${tag}`,   // inline, horizontal: tag above
    `<rect class="sym filter" x="${x - 10}" y="${y - 17}" width="20" height="34"/><line class="sym dashed" x1="${x}" y1="${y - 17}" x2="${x}" y2="${y + 17}"/>` +
    label(x, y - 26, tag), `${tag} · Filter`);

  const temp = (id, x, y, tag, name, stubX, stubY, left) => {   // tag inside the circle
    const bx = left ? x - 118 : x + 22;
    return group(id,
      `<line class="sym" x1="${x}" y1="${y + (stubY > y ? 18 : -18)}" x2="${stubX}" y2="${stubY}"/>` +
      `<circle class="sym" cx="${x}" cy="${y}" r="18"/><text class="sym-text temp-tag" x="${x}" y="${y + 5}" text-anchor="middle">${esc(tag)}</text>` +
      `<rect class="value-box" data-box="${id}" x="${bx}" y="${y - 14}" width="96" height="28" rx="5"/>` +
      `<text class="value" data-value="${id}" x="${bx + 48}" y="${y + 6}" text-anchor="middle">—</text>`,
      `${tag} · ${name}`);
  };

  /* EDB ---------------------------------------------------------------------- */
  function edibl() {
    const L1 = 359, L2 = 489, H = 609, P = 1020;   // line 1, line 2, drain header, pumps
    const E = C.effluent;
    const down = (x, from, jumpAt) => jumpAt   // vertical branch to the drain header
      ? pipe([[x, from], [x, jumpAt - 8]], E) + jump(x, jumpAt, E) + pipe([[x, jumpAt + 8], [x, H]], E)
      : pipe([[x, from], [x, H]], E);
    return `<svg viewBox="0 0 ${W} ${H_VIEW}" class="process" role="img" aria-label="EDB process view">
      ${arrow(20, L1, 90, "Tank #1", "#3a3f46")}${arrow(20, L2, 90, "Tank #2", "#3a3f46")}
      ${pipe([[110, L1], [960, L1], [960, 325], [P - 16, 325]], E)}
      ${pipe([[110, L2], [P - 16, L2]], E)}
      ${pipe([[305, 70], [305, L1]], E)}${pipe([[325, 70], [325, L1 - 8]], E)}
      ${jump(325, L1, E)}${pipe([[325, L1 + 8], [325, L2]], E)}
      ${down(350, L1, L2)}${down(455, L1, L2)}${down(540, L2)}${down(670, L2)}
      ${pipe([[910, L1], [910, L2]], E)}
      ${pipe([[350, H], [1270, H]], E)}
      ${pipe([[P + 22, 313], [1110, 313], [1110, 95], [1270, 95]], E)}
      ${pipe([[P + 22, L2 - 12], [1150, L2 - 12], [1150, 150], [1270, 150]], E)}
      ${pipe([[518, 690], [P - 316, 690]], C.water)}${pipe([[742, 678], [820, 678], [820, H]], C.water)}
      ${arrow(1272, 95, 88, "PST-TK-01", E)}${arrow(1272, 150, 88, "PST-TK-01", E)}
      ${arrow(1272, H, 88, "Drain Pool", "#3d6b2a")}
      ${label(300, 52, "Tank #1 switches", "name", "end")}${label(330, 52, "Tank #2 switches", "name", "start")}
      ${levelSwitch("level_switch_6", 262, 95, "LSHH", "EDB-LS-06", "Tank 1 overflow", 305, "left")}
      ${levelSwitch("level_switch_4", 262, 190, "LSH", "EDB-LS-04", "Tank 1 high", 305, "left")}
      ${levelSwitch("level_switch_2", 262, 285, "LSL", "EDB-LS-02", "Tank 1 low", 305, "left")}
      ${levelSwitch("level_switch_7", 372, 95, "LSHH", "EDB-LS-07", "Tank 2 overflow", 325, "right")}
      ${levelSwitch("level_switch_5", 372, 190, "LSH", "EDB-LS-05", "Tank 2 high", 325, "right")}
      ${levelSwitch("level_switch_3", 372, 285, "LSL", "EDB-LS-03", "Tank 2 low", 325, "right")}
      ${manual(350, (L1 + L2) / 2, "EDB-VL-03", true)}
      ${motorValve("overflow_valve_1_open", 455, (L1 + L2) / 2, "EDB-VL-05", "Overflow Y1")}
      ${manual(540, (L2 + H) / 2, "EDB-VL-04", true)}
      ${motorValve("overflow_valve_2_open", 670, (L2 + H) / 2, "EDB-VL-06", "Overflow Y2")}
      ${filter(700, L1, "EDB-FT-01")}${manual(780, L1, "EDB-VL-09", false)}${filter(860, L1, "EDB-FT-03")}
      ${filter(700, L2, "EDB-FT-02")}${manual(780, L2, "EDB-VL-10", false)}${filter(860, L2, "EDB-FT-04")}
      ${manual(910, (L1 + L2) / 2, "EDB-VL-13", true, undefined, "left")}
      ${pump("transfer_pump_1", P, 325, "EDB-PU-01", "Transfer pump 1")}
      ${pump("transfer_pump_2", P, L2, "EDB-PU-02", "Transfer pump 2")}
      ${flowMeter("flow_sensor_1", 1065, 215, "EDB-FR-01", "Transfer flow, line 1", 1110, "left")}
      ${flowMeter("flow_sensor_2", 1195, 215, "EDB-FR-02", "Transfer flow, line 2", 1150, "right")}
      ${levelSwitch("level_switch_1", 500, 690, "LS", "EDB-LS-01", "Manhole water present", 500, "left")}
      ${filter(610, 690, "EDB-FT-05")}
      ${pump("manhole_drain", 720, 690, "EDB-PU-03", "Manhole drain pump")}
    </svg>`;
  }

  /* Pasteurizer -------------------------------------------------------------- */
  const VX = 700, R = 900, VT = 262, VB = 510;   // PST-HX-02: left/right wall, top, bottom

  // Coil of effluent inside PST-HX-02: in at the bottom left port, out at the top left port.
  function vessel() {
    const pts = [[VX, 469], [820, 469]];
    for (let i = 1; i <= 6; i++) pts.push([i % 2 ? 882 : 820, 469 - i * 27]);
    pts.push([VX, 307]);
    return `<rect class="sym" x="${VX}" y="${VT}" width="${R - VX}" height="${VB - VT}" rx="30"/>` +
      pipe(pts, C.filterEff, "thin") +
      label((VX + R) / 2, VB + 20, "PST-HX-02") + label((VX + R) / 2, VB + 36, "Pasteurizer vessel", "name");
  }

  // Immersion heater through the left wall of PST-HX-02: terminal box outside,
  // element in the water; command, tag and name left of the terminal box.
  const heater = (id, y, len, tag, name) => {
    const lx = VX - 36;
    return group(id,
      `<rect class="sym" x="${VX - 28}" y="${y - 11}" width="20" height="22"/>` +
      `<text class="sym-text small" x="${VX - 18}" y="${y + 4}" text-anchor="middle">E</text>` +
      `<line class="sym" x1="${VX - 8}" y1="${y}" x2="${VX}" y2="${y}"/>` +
      `<rect class="sym heater-rod" x="${VX}" y="${y - 5}" width="${len}" height="10" rx="5"/>` +
      label(lx, y - 18, tag, "tag", "end") + lamp(id, lx - 8, y - 1) + state(id, lx - 22, y + 4, "end") +
      label(lx, y + 18, name, "name", "end"),
      `${tag} · ${name}`);
  };

  function pasteurizer() {
    const HXL = 400, HXR = 455;          // GPST-HX-01 left and right ports
    const BX = 1090, PU = 1000;          // burner pipe, circulation pump
    const Y4 = 1100, Y3 = 1170, LINE = 660, DRAIN = 720, BR = 1000, END = 1236;
    return `<svg viewBox="0 0 ${W} ${H_VIEW}" class="process" role="img" aria-label="Pasteurizer process view">
      ${arrow(22, 98, 80, "EDB", "#7a4a1a")}
      ${pipe([[126, 98], [137, 98]], C.filterEff)}
      ${pipe([[137, 160], [88, 160], [88, 305], [HXL, 305]], C.filterEff)}
      ${pipe([[VX, 307], [HXR, 307]], C.past)}
      ${pipe([[HXR, 395], [497, 395], [497, 469], [VX, 469]], C.filterEff)}
      ${pipe([[HXL, 395], [310, 395], [310, LINE], [END, LINE]], C.past)}
      ${pipe([[BR, LINE], [BR, DRAIN], [END, DRAIN]], C.past)}
      ${pipe([[R, 307], [BX, 307], [BX, 342]], C.hot)}${pipe([[BX, 399], [BX, 459], [PU + 16, 459]], C.hot)}
      ${pipe([[R, 469], [PU - 16, 469]], C.hot)}
      ${pipe([[1236, 440], [BX + 30, 440], [BX + 30, 399]], C.biogas)}
      <polygon class="terminal" points="${BX + 22},${409} ${BX + 30},${399} ${BX + 38},${409}" fill="${C.biogas}"/>
      ${label(1244, 444, "Biogas supply", "name", "start")}
      ${outlet(END, LINE, C.past)}${label(END + 22, LINE + 4, "Treated effluent out", "name", "start")}
      ${outlet(END, DRAIN, C.past)}${label(END + 22, DRAIN + 4, "Effluent manual drain", "name", "start")}
      ${group("tank_PST-TK-01", `<rect class="sym" x="137" y="70" width="173" height="108"/><rect class="hatch" x="120" y="178" width="208" height="12"/>` +
        label(224, 112, "1000 L sludge", "name") + label(224, 128, "buffer tank", "name") + label(224, 150, "PST-TK-01"),
        "PST-TK-01 · 1000 L sludge buffer tank")}
      ${levelSwitch("level_switch_9", 374, 84, "LSH", "PST-LS-02", "Buffer tank high", 310, "right")}
      ${levelSwitch("level_switch_8", 374, 158, "LSL", "PST-LS-01", "Buffer tank low", 310, "right")}
      ${flowMeter("flow_sensor_3", 180, 348, "PST-FI-01", "Pasteurizer feed flow", { y: 305 }, "right")}
      ${group("hx_GPST-HX-01", `<rect class="sym" x="400" y="290" width="55" height="115"/>` +
        pipe([[HXL, 305], [HXR, 395]], C.filterEff, "thin") + pipe([[HXR, 307], [HXL, 395]], C.past, "thin") +
        label(428, 424, "GPST-HX-01") + label(428, 440, "Heat recovery", "name"), "GPST-HX-01 · Heat recovery exchanger")}
      ${vessel()}
      ${heater("heater_3kw", 350, 105, "GPST-EH-01", "3 kW startup heater")}
      ${heater("heater_1kw", 428, 75, "GPST-EH-02", "1 kW assist heater")}
      ${temp("PT1_B2", 352, 262, "PT1", "Cold effluent into GPST-HX-01", 352, 305, true)}
      ${temp("PT4_B5", 352, 470, "PT4", "Cooled treated effluent", 352, 395, false)}
      ${temp("PT3_B4", 660, 250, "PT3", "Hot effluent leaving PST-HX-02", 660, 307, true)}
      ${temp("PT2_B3", 660, 540, "PT2", "Warm effluent into PST-HX-02", 660, 469, true)}
      ${temp("PT6_B7", 960, 250, "PT6", "Hot water from the burner", 960, 307, false)}
      ${temp("PT5_B6", 930, 556, "PT5", "Hot water to the burner", 930, 469, false)}
      ${pump("heating_circulation", PU, 467, "GPST-PU-01", "Circulation pump")}
      ${group("burner", `<rect class="sym" x="${BX - 13}" y="342" width="57" height="57"/><path class="sym" d="M${BX + 7} 352l-8 6l8 6l-8 6l8 6l-8 6l8 6"/>` +
        label(BX + 62, 362, "GPST-GB-01", "tag", "start") + label(BX + 62, 378, "Biogas water heater", "name", "start") +
        label(BX + 62, 394, "Flame not monitored", "note", "start"), "GPST-GB-01 · Biogas water heater (no flame signal)")}
      ${group("control_valve_percent", bowtie(Y4, LINE, false) + `<line class="sym" x1="${Y4}" y1="${LINE - 10}" x2="${Y4}" y2="${LINE - 32}"/><circle class="sym" cx="${Y4}" cy="${LINE - 50}" r="18"/>` +
        `<text class="sym-text small" x="${Y4}" y="${LINE - 45}" text-anchor="middle">%</text>` +
        label(Y4 - 28, LINE - 54, "GPST-CVL-02", "tag", "end") + label(Y4 - 28, LINE - 38, "Control valve Y4", "name", "end") +
        `<text class="y4" data-y4="req" x="${BR - 12}" y="${LINE + 28}" text-anchor="end">Requested —</text>` +
        `<text class="y4" data-y4="wr" x="${BR - 12}" y="${LINE + 46}" text-anchor="end">Written —</text>` +
        `<text class="y4" data-y4="fb" x="${BR - 12}" y="${LINE + 64}" text-anchor="end">Measured —</text>`, "GPST-CVL-02 · Treatment control valve Y4")}
      ${group("pasteurizer_isolation_open", bowtie(Y3, LINE, false) + `<line class="sym" x1="${Y3}" y1="${LINE - 10}" x2="${Y3}" y2="${LINE - 32}"/><circle class="sym" cx="${Y3}" cy="${LINE - 48}" r="16"/>` +
        `<text class="sym-text" x="${Y3}" y="${LINE - 43}" text-anchor="middle">M</text>` +
        command("pasteurizer_isolation_open", Y3, LINE - 96) +
        label(Y3 + 26, LINE - 52, "GPST-VL-08", "tag", "start") + label(Y3 + 26, LINE - 36, "Isolation valve Y3", "name", "start"),
        "GPST-VL-08 · Pasteurizer isolation valve Y3 (command only, no position feedback)")}
      ${manual(Y3, DRAIN, "GPST-VL-07", false, "Manual drain valve — position not monitored")}
      <g class="legend" transform="translate(1196 30)"><rect width="190" height="118" rx="6"/>
        <text class="legend-title" x="12" y="22">Legend (fluid)</text>
        ${[["Hot water", C.hot], ["Pasteurised effluent", C.past], ["Filter effluent", C.filterEff], ["Biogas", C.biogas]]
          .map(([t, c], i) => `<line x1="12" x2="44" y1="${42 + i * 20}" y2="${42 + i * 20}" stroke="${c}" stroke-width="4"/><text class="legend-text" x="52" y="${46 + i * 20}">${t}</text>`).join("")}
      </g>
    </svg>`;
  }

  function legendEdibl() {
    return `<div class="legend-strip"><b>Legend (fluid, not measured flow):</b>
      <span><i style="background:${C.effluent}"></i>Effluent</span>
      <span><i style="background:${C.water}"></i>Water</span></div>`;
  }

  return { edibl, pasteurizer, legendEdibl };
})();
