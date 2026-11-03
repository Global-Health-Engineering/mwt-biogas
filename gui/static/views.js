/* Process drawings (SVG) after GUI_V2: arrangement, tags and pipe colours kept.
   Dynamic parts carry data-id (software id) and get values from app.js. */
"use strict";

const VIEWS = (() => {
  const C = {
    tankLine: "#B20000", toBuffer: "#C73500", filterEff: "#b46504", hot: "#FF0000",
    past: "#2D7600", biogas: "#B09500", drain: "#82b366",
  };
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
  // Equipment groups: clickable, lamp + state text filled in by app.js.
  const lamp = (id, x, y) => `<circle class="lamp unknown" data-lamp="${id}" cx="${x}" cy="${y}" r="9"/>`;
  const state = (id, x, y, anchor = "start") =>
    `<text class="state" data-state="${id}" x="${x}" y="${y}" text-anchor="${anchor}">—</text>`;
  const detail = (id, x, y, anchor = "start") =>
    `<text class="detail" data-detail="${id}" x="${x}" y="${y}" text-anchor="${anchor}"></text>`;
  const group = (id, inner, title) =>
    `<g class="equip" data-id="${id}" tabindex="0"><title>${esc(title)}</title>${inner}</g>`;

  // Lamp, command text and (when an interlock overrides it) the requested
  // value on a second line. stateLeft: text to the left of the lamp.
  const pump = (id, x, y, tag, name, box, right, stateLeft) => {
    const lx = box ? x - 36 : stateLeft ? x + 16 : x, ly = y - 44;
    const sx = stateLeft ? lx - 14 : lx + 14, anchor = stateLeft ? "end" : "start";
    return group(id,
    (box ? `<rect class="box" x="${box[0]}" y="${box[1]}" width="${box[2]}" height="${box[3]}"/>` : "") +
    `<circle class="sym" cx="${x}" cy="${y}" r="16"/><path class="sym" d="M${x} ${y - 16}h22v10h-6"/>` +
    `<path class="sym" d="M${x - 10} ${y + 12}l-6 10h32l-6 -10"/>` +
    lamp(id, lx, ly) + state(id, sx, ly + 5, anchor) +
    `<text class="detail" data-req="${id}" x="${sx}" y="${ly + 21}" text-anchor="${anchor}"></text>` +
    (right ? label(x + 30, y + 28, tag, "tag", "start") + label(x + 30, y + 44, name, "name", "start")
           : label(x, y + 42, tag) + label(x, y + 58, name, "name")), `${tag} · ${name}`);
  };

  const bowtie = (x, y, vertical) => vertical
    ? `<path class="sym valve" d="M${x - 10} ${y - 14}h20l-20 28h20z"/>`
    : `<path class="sym valve" d="M${x - 14} ${y - 10}v20l28 -20v20z"/>`;

  const manual = (x, y, tag, vertical, lx, ly, anchor = "start") => group(`manual_${tag}`,
    bowtie(x, y, vertical) + label(lx, ly, tag, "tag manual-tag", anchor),
    `${tag} · Manual valve — position not monitored`);

  const motorValve = (id, x, y, tag, name) => {
    const ax = x + 40;
    return group(id,
      bowtie(x, y, true) + `<line class="sym" x1="${x + 10}" y1="${y}" x2="${ax - 14}" y2="${y}"/>` +
      `<circle class="sym" cx="${ax}" cy="${y}" r="14"/><text class="sym-text" x="${ax}" y="${y + 5}" text-anchor="middle">M</text>` +
      lamp(id, ax, y + 26) + state(id, ax + 14, y + 31) +
      label(x - 14, y - 4, tag, "tag", "end") + label(x - 14, y + 12, name, "name", "end"),
      `${tag} · ${name} (command only, no position feedback)`);
  };

  const levelSwitch = (id, x, y, type, tag, name, stubTo) => group(id,
    (stubTo === x ? "" : `<line class="sym" x1="${x + (stubTo > x ? 18 : -18)}" y1="${y}" x2="${stubTo}" y2="${y}"/>`) +
    `<circle class="sym switch" data-switch="${id}" cx="${x}" cy="${y}" r="18"/>` +
    `<text class="sym-text small" x="${x}" y="${y + 4}" text-anchor="middle">${type}</text>` +
    label(x, y + 34, tag) + state(id, x, y + 50, "middle"),
    `${tag} · ${name}`);

  const flowMeter = (id, x, y, tag, name, toX, side) => {   // side: "left" | "right" | "below"
    const below = side === "below";
    const lx = below ? x : side === "left" ? x - 26 : x + 26;
    const ly = below ? y + 36 : y - 6;
    const anchor = below ? "middle" : side === "left" ? "end" : "start";
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

  const filter = (x, y, tag, vertical, ly) => group(`filter_${tag}`,
    `<rect class="sym filter" x="${x - 10}" y="${y - 17}" width="20" height="34"/><line class="sym dashed" x1="${x}" y1="${y - 17}" x2="${x}" y2="${y + 17}"/>` +
    label(x, ly, tag), `${tag} · Filter`);

  const temp = (id, x, y, tag, name, stubX, stubY, left) => {   // tag inside the circle
    const bx = left ? x - 118 : x + 22;
    return group(id,
      `<line class="sym" x1="${x}" y1="${y + (stubY > y ? 18 : -18)}" x2="${stubX}" y2="${stubY}"/>` +
      `<circle class="sym" cx="${x}" cy="${y}" r="18"/><text class="sym-text temp-tag" x="${x}" y="${y + 5}" text-anchor="middle">${esc(tag)}</text>` +
      `<rect class="value-box" data-box="${id}" x="${bx}" y="${y - 14}" width="96" height="28" rx="5"/>` +
      `<text class="value" data-value="${id}" x="${bx + 48}" y="${y + 6}" text-anchor="middle">—</text>`,
      `${tag} · ${name}`);
  };

  function edibl() {
    const L1 = 359, L2 = 449, H = 527;
    return `<svg viewBox="0 0 1300 760" class="process" role="img" aria-label="EDiBl process view">
      ${arrow(20, L1, 90, "Tank #1", "#3a3f46")}${arrow(20, L2, 90, "Tank #2", "#3a3f46")}
      ${pipe([[110, L1], [848, L1], [848, 325], [904, 325]], C.tankLine)}
      ${pipe([[110, L2], [904, L2]], C.tankLine)}
      ${pipe([[305, 70], [305, L1]], C.tankLine)}${pipe([[325, 70], [325, L1 - 8]], C.tankLine)}
      ${jump(325, L1, C.tankLine)}${pipe([[325, L1 + 8], [325, L2]], C.tankLine)}
      ${pipe([[363, L1], [363, L2 - 8]], C.tankLine)}${jump(363, L2, C.tankLine)}${pipe([[363, L2 + 8], [363, H]], C.tankLine)}
      ${pipe([[387, L2], [387, H]], C.tankLine)}
      ${pipe([[523, L1], [523, L2 - 8]], C.tankLine)}${jump(523, L2, C.tankLine)}${pipe([[523, L2 + 8], [523, H]], C.tankLine)}
      ${pipe([[500, L2], [500, H]], C.tankLine)}
      ${pipe([[808, L1], [808, L2]], C.tankLine)}
      ${pipe([[363, H], [1063, H], [1063, 487], [1170, 487]], C.tankLine)}
      ${pipe([[936, 313], [1075, 313], [1075, 143], [1170, 143]], C.toBuffer)}
      ${pipe([[936, 437], [1108, 437], [1108, 209], [1170, 209]], C.toBuffer)}
      ${pipe([[560, 690], [684, 690]], C.filterEff)}${pipe([[716, 678], [795, 678], [795, H]], C.filterEff)}
      ${arrow(1172, 143, 88, "PST-TK-01", "#C73500")}${arrow(1172, 209, 88, "PST-TK-01", "#C73500")}
      ${arrow(1172, 487, 88, "Drain Pool", "#3d6b2a")}
      ${label(250, 52, "Tank #1 switches", "name")}${label(380, 52, "Tank #2 switches", "name")}
      ${levelSwitch("level_switch_6", 262, 95, "LSHH", "EDB-LS-06", "Tank 1 overflow", 305)}
      ${levelSwitch("level_switch_4", 262, 190, "LSH", "EDB-LS-04", "Tank 1 high", 305)}
      ${levelSwitch("level_switch_2", 262, 285, "LSL", "EDB-LS-02", "Tank 1 low", 305)}
      ${levelSwitch("level_switch_7", 372, 95, "LSHH", "EDB-LS-07", "Tank 2 overflow", 325)}
      ${levelSwitch("level_switch_5", 372, 190, "LSH", "EDB-LS-05", "Tank 2 high", 325)}
      ${levelSwitch("level_switch_3", 372, 285, "LSL", "EDB-LS-03", "Tank 2 low", 325)}
      ${manual(363, 398, "EDB-VL-03", true, 349, 403, "end")}
      ${manual(387, 488, "EDB-VL-04", true, 373, 512, "end")}
      ${motorValve("overflow_valve_1_open", 523, 398, "EDB-VL-05", "Overflow Y1")}
      ${motorValve("overflow_valve_2_open", 500, 488, "EDB-VL-06", "Overflow Y2")}
      ${filter(610, L1, "EDB-FT-01", false, 330)}${manual(700, L1, "EDB-VL-09", false, 700, 392, "middle")}
      ${filter(760, L1, "EDB-FT-03", false, 330)}
      ${filter(650, L2, "EDB-FT-02", false, 484)}${manual(735, L2, "EDB-VL-10", false, 735, 428, "middle")}
      ${filter(775, L2, "EDB-FT-04", false, 484)}
      ${manual(808, 404, "EDB-VL-13", true, 794, 409, "end")}
      ${pump("transfer_pump_1", 920, 325, "EDB-PU-01", "Transfer pump 1", [868, 262, 128, 100])}
      ${pump("transfer_pump_2", 920, 449, "EDB-PU-02", "Transfer pump 2", [868, 386, 128, 100])}
      ${flowMeter("flow_sensor_1", 1030, 181, "EDB-FR-01", "Transfer flow, line 1", 1075, "left")}
      ${flowMeter("flow_sensor_2", 1150, 294, "EDB-FR-02", "Transfer flow, line 2", 1108, "right")}
      ${filter(612, 690, "EDB-FT-05", false, 726)}
      ${levelSwitch("level_switch_1", 560, 610, "LS", "EDB-LS-01", "Manhole water present", 560)}
      ${pump("manhole_drain", 700, 690, "EDB-PU-03", "Manhole drain pump", [656, 626, 120, 112], true)}
    </svg>`;
  }

  const D = 80;   // the vessel and everything right of it, shifted to free the heater labels

  function vessel() {
    let coil = `M${650 + D} 290`;
    for (let i = 0; i < 6; i++) coil += ` L${708 + D} ${306 + i * 30} L${650 + D} ${321 + i * 30}`;
    return `<rect class="sym" x="${638 + D}" y="262" width="82" height="241" rx="30"/>` +
      `<path class="pipe thin" d="${coil}" stroke="${C.filterEff}"/>` +
      label(679 + D, 522, "PST-HX-02") + label(679 + D, 538, "Pasteurizer vessel", "name");
  }

  const heater = (id, x, y, w, tag, name, ly) => group(id,
    `<rect class="sym" x="${x}" y="${y}" width="${w}" height="${w * 0.7}"/>` +
    `<rect class="sym" x="${x - 26}" y="${y + 4}" width="20" height="${w * 0.7 - 8}"/>` +
    `<text class="sym-text small" x="${x - 16}" y="${y + w * 0.35 + 4}" text-anchor="middle">E</text>` +
    `<line class="sym" x1="${x - 6}" y1="${y + w * 0.35}" x2="${x}" y2="${y + w * 0.35}"/>` +
    label(x - 40, ly - 16, tag, "tag", "end") + lamp(id, x - 50, ly) + state(id, x - 64, ly + 5, "end") +
    label(x - 40, ly + 22, name, "name", "end"),
    `${tag} · ${name}`);

  function pasteurizer() {
    const VX = 638 + D, R = 720 + D;    // vessel wall, left and right (pipes end on it)
    const HXL = 400, HXR = 455;          // GPST-HX-01 left and right ports
    return `<svg viewBox="0 0 1300 760" class="process" role="img" aria-label="Pasteurizer process view">
      ${arrow(22, 98, 80, "EDiBl", "#7a4a1a")}
      ${pipe([[126, 98], [137, 98]], C.filterEff)}
      ${pipe([[137, 160], [88, 160], [88, 305], [HXL, 305]], C.filterEff)}
      ${pipe([[VX, 307], [HXR, 307]], C.past)}
      ${pipe([[HXR, 395], [497, 395], [497, 469], [VX, 469]], C.filterEff)}
      ${pipe([[HXL, 395], [310, 395], [310, 660], [1170, 660]], C.past)}
      ${pipe([[925, 660], [925, 720], [1070, 720], [1070, 660]], C.past)}
      ${pipe([[R, 307], [898 + D, 307], [898 + D, 342]], C.hot)}${pipe([[898 + D, 399], [898 + D, 459], [852 + D, 459]], C.hot)}
      ${pipe([[R, 469], [820 + D, 469]], C.hot)}
      <polygon class="terminal" points="1170,652 1186,660 1170,668" fill="${C.past}"/>
      ${label(1150, 686, "Treated effluent out", "name")}
      ${group("tank_PST-TK-01", `<rect class="sym" x="137" y="70" width="173" height="108"/><rect class="hatch" x="120" y="178" width="208" height="12"/>` +
        label(224, 112, "1000 L sludge", "name") + label(224, 128, "buffer tank", "name") + label(224, 150, "PST-TK-01"),
        "PST-TK-01 · 1000 L sludge buffer tank")}
      ${levelSwitch("level_switch_9", 374, 84, "LSH", "PST-LS-02", "Buffer tank high", 310)}
      ${levelSwitch("level_switch_8", 374, 158, "LSL", "PST-LS-01", "Buffer tank low", 310)}
      ${flowMeter("flow_sensor_3", 180, 348, "PST-FI-01", "Pasteurizer feed flow", { y: 305 }, "right")}
      ${group("hx_GPST-HX-01", `<rect class="sym" x="400" y="290" width="55" height="115"/><line class="sym" x1="400" y1="290" x2="455" y2="405"/><line class="sym" x1="455" y1="290" x2="400" y2="405"/>` +
        label(428, 424, "GPST-HX-01") + label(428, 440, "Heat recovery", "name"), "GPST-HX-01 · Heat recovery exchanger")}
      ${vessel()}
      ${temp("PT1_B2", 352, 262, "PT1", "Cold effluent into GPST-HX-01", 352, 305, true)}
      ${temp("PT4_B5", 352, 470, "PT4", "Cooled treated effluent", 352, 395, false)}
      ${temp("PT3_B4", 593 + D, 250, "PT3", "Hot effluent leaving PST-HX-02", 593 + D, 307, true)}
      ${temp("PT2_B3", 595 + D, 540, "PT2", "Warm effluent into PST-HX-02", 595 + D, 469, true)}
      ${temp("PT6_B7", 758 + D, 250, "PT6", "Hot water from the burner", 758 + D, 307, false)}
      ${temp("PT5_B6", 770 + D, 540, "PT5", "Hot water to the burner", 770 + D, 469, false)}
      ${heater("heater_3kw", 594 + D, 350, 44, "GPST-EH-01", "3 kW startup heater", 360)}
      ${heater("heater_1kw", 604 + D, 412, 30, "GPST-EH-02", "1 kW assist heater", 430)}
      ${pump("heating_circulation", 836 + D, 467, "GPST-PU-01", "Circulation pump", null, true, true)}
      ${group("burner", `<rect class="sym" x="${885 + D}" y="342" width="57" height="57"/><path class="sym" d="M${905 + D} 352l-8 6l8 6l-8 6l8 6l-8 6l8 6"/>` +
        label(960 + D, 362, "GPST-GB-01", "tag", "start") + label(960 + D, 378, "Biogas water heater", "name", "start") +
        label(960 + D, 394, "Flame not monitored", "note", "start"), "GPST-GB-01 · Biogas water heater (no flame signal)")}
      ${group("control_valve_percent", bowtie(978, 660, false) + `<line class="sym" x1="978" y1="650" x2="978" y2="628"/><circle class="sym" cx="978" cy="610" r="18"/>` +
        `<text class="sym-text small" x="978" y="615" text-anchor="middle">%</text>` +
        label(950, 606, "GPST-CVL-02", "tag", "end") + label(950, 622, "Control valve Y4", "name", "end") +
        `<text class="y4" data-y4="req" x="905" y="688" text-anchor="end">Requested —</text>` +
        `<text class="y4" data-y4="wr" x="905" y="706" text-anchor="end">Written —</text>` +
        `<text class="y4" data-y4="fb" x="905" y="724" text-anchor="end">Measured —</text>`, "GPST-CVL-02 · Treatment control valve Y4")}
      ${group("pasteurizer_isolation_open", bowtie(1030, 660, false) + `<line class="sym" x1="1030" y1="650" x2="1030" y2="628"/><circle class="sym" cx="1030" cy="612" r="16"/>` +
        `<text class="sym-text" x="1030" y="617" text-anchor="middle">M</text>` + lamp("pasteurizer_isolation_open", 1060, 612) +
        state("pasteurizer_isolation_open", 1076, 617) + label(1030, 582, "GPST-VL-08", "tag") + label(1030, 566, "Isolation valve Y3", "name"),
        "GPST-VL-08 · Pasteurizer isolation valve Y3 (command only, no position feedback)")}
      ${manual(997, 720, "GPST-VL-07", false, 997, 746, "middle")}
      <g class="legend" transform="translate(1110 40)"><rect width="176" height="118" rx="6"/>
        <text class="legend-title" x="12" y="22">Legend (fluid)</text>
        ${[["Hot water", C.hot], ["Pasteurised effluent", C.past], ["Filter effluent", C.filterEff], ["Biogas", C.biogas]]
          .map(([t, c], i) => `<line x1="12" x2="44" y1="${42 + i * 20}" y2="${42 + i * 20}" stroke="${c}" stroke-width="4"/><text class="legend-text" x="52" y="${46 + i * 20}">${t}</text>`).join("")}
      </g>
    </svg>`;
  }

  function legendEdibl() {
    return `<div class="legend-strip"><b>Legend (fluid, not measured flow):</b>
      <span><i style="background:${C.tankLine}"></i>Effluent, tanks to pumps</span>
      <span><i style="background:${C.toBuffer}"></i>Effluent, pumps to PST-TK-01</span>
      <span><i style="background:${C.filterEff}"></i>Filter effluent (drain pump)</span></div>`;
  }

  return { edibl, pasteurizer, legendEdibl };
})();
