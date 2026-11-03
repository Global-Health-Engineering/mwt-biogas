"""Drawing tags, readable names and software ids for the two process views.

Tags are exactly those shown in GUI_V2 (and the P&ID). All dynamic mappings
were confirmed on 3 Nov 2026. `kind` says what the GUI may claim:

  command          output only: requested / commanded / written, no feedback
  command+feedback output with measured position (Y4)
  measured         a reading from the plant
  not_monitored    drawn, but the controller has no signal for it
  manual           manual valve: "Manual - position not monitored", no controls
  static           tank, heat exchanger or filter: reference symbol only
"""

CONFIRMED = "2026-11-03"


def _e(id_, tag, name, view, kind, units=None):
    return {"id": id_, "tag": tag, "name": name, "view": view, "kind": kind,
            "units": units, "confirmed": CONFIRMED}


EQUIPMENT = [
    # EDiBl (GUI_V2 page 1)
    _e("transfer_pump_1", "EDB-PU-01", "Transfer pump, line 1", "edibl", "command"),
    _e("transfer_pump_2", "EDB-PU-02", "Transfer pump, line 2", "edibl", "command"),
    _e("manhole_drain", "EDB-PU-03", "Manhole drain pump", "edibl", "command"),
    _e("flow_sensor_1", "EDB-FR-01", "Transfer flow, line 1", "edibl", "measured", "L/min"),
    _e("flow_sensor_2", "EDB-FR-02", "Transfer flow, line 2", "edibl", "measured", "L/min"),
    _e("overflow_valve_1_open", "EDB-VL-05", "Overflow valve Y1, tank 1", "edibl", "command"),
    _e("overflow_valve_2_open", "EDB-VL-06", "Overflow valve Y2, tank 2", "edibl", "command"),
    _e("level_switch_1", "EDB-LS-01", "Manhole water present", "edibl", "measured"),
    _e("level_switch_2", "EDB-LS-02", "Tank 1 low", "edibl", "measured"),
    _e("level_switch_3", "EDB-LS-03", "Tank 2 low", "edibl", "measured"),
    _e("level_switch_4", "EDB-LS-04", "Tank 1 high", "edibl", "measured"),
    _e("level_switch_5", "EDB-LS-05", "Tank 2 high", "edibl", "measured"),
    _e("level_switch_6", "EDB-LS-06", "Tank 1 overflow", "edibl", "measured"),
    _e("level_switch_7", "EDB-LS-07", "Tank 2 overflow", "edibl", "measured"),
    *[_e(f"manual_{t}", t, "Manual valve", "edibl", "manual")
      for t in ("EDB-VL-03", "EDB-VL-04", "EDB-VL-09", "EDB-VL-10", "EDB-VL-13")],
    *[_e(f"filter_{t}", t, "Filter", "edibl", "static")
      for t in ("EDB-FT-01", "EDB-FT-02", "EDB-FT-03", "EDB-FT-04", "EDB-FT-05")],
    # Pasteurizer (GUI_V2 page 2)
    _e("PT1_B2", "PT1", "Cold effluent into GPST-HX-01", "pasteurizer", "measured", "°C"),
    _e("PT2_B3", "PT2", "Warm effluent into PST-HX-02", "pasteurizer", "measured", "°C"),
    _e("PT3_B4", "PT3", "Hot effluent leaving PST-HX-02 (control)", "pasteurizer", "measured",
       "°C"),
    _e("PT4_B5", "PT4", "Cooled treated effluent", "pasteurizer", "measured", "°C"),
    _e("PT5_B6", "PT5", "Hot water to the burner", "pasteurizer", "measured", "°C"),
    _e("PT6_B7", "PT6", "Hot water from the burner", "pasteurizer", "measured", "°C"),
    _e("flow_sensor_3", "PST-FI-01", "Pasteurizer feed flow", "pasteurizer", "measured",
       "L/min"),
    _e("level_switch_8", "PST-LS-01", "Buffer tank low (treatment supply)", "pasteurizer",
       "measured"),
    _e("level_switch_9", "PST-LS-02", "Buffer tank high", "pasteurizer", "measured"),
    _e("heater_3kw", "GPST-EH-01", "3 kW startup heater", "pasteurizer", "command"),
    _e("heater_1kw", "GPST-EH-02", "1 kW assist heater", "pasteurizer", "command"),
    _e("heating_circulation", "GPST-PU-01", "Hot-water circulation pump", "pasteurizer",
       "command"),
    _e("control_valve_percent", "GPST-CVL-02", "Treatment control valve Y4", "pasteurizer",
       "command+feedback", "%"),
    _e("pasteurizer_isolation_open", "GPST-VL-08", "Pasteurizer isolation valve Y3",
       "pasteurizer", "command"),
    _e("burner", "GPST-GB-01", "Biogas water heater (flame not monitored)", "pasteurizer",
       "not_monitored"),
    _e("manual_GPST-VL-07", "GPST-VL-07", "Manual bypass valve", "pasteurizer", "manual"),
    _e("tank_PST-TK-01", "PST-TK-01", "1000 L sludge buffer tank", "pasteurizer", "static"),
    _e("hx_GPST-HX-01", "GPST-HX-01", "Heat recovery exchanger", "pasteurizer", "static"),
    _e("hx_PST-HX-02", "PST-HX-02", "Pasteurizer vessel", "pasteurizer", "static"),
    # Plant-wide (header and details)
    _e("cabinet_fan_disabled", "M1", "Cabinet fan (thermostat control unless disabled)",
       "plant", "command"),
    _e("biogas_compressor", "M3.3", "Biogas compressor (held off)", "plant", "command"),
    _e("mode_selector", "S1", "Mode selector", "plant", "measured"),
    # Cabinet lights: no P&ID tag, shown by name only
    _e("light_green", "Green light", "Cabinet light: pasteurizing", "plant", "command"),
    _e("light_yellow", "Yellow light", "Cabinet light: warning", "plant", "command"),
    _e("light_red", "Red light", "Cabinet light: fault / fallback path", "plant", "command"),
]

for _light in EQUIPMENT[-3:]:
    _light["confirmed"] = None     # display names only, not a P&ID mapping

BY_ID = {e["id"]: e for e in EQUIPMENT}

# Pipe colours as in GUI_V2 (fluid identity, never measured flow).
LEGEND = {
    "edibl": [
        {"colour": "#B20000", "label": "Effluent, tanks to transfer pumps"},
        {"colour": "#C73500", "label": "Effluent, pumps to PST-TK-01"},
        {"colour": "#b46504", "label": "Filter effluent (drain pump)"},
    ],
    "pasteurizer": [
        {"colour": "#FF0000", "label": "Hot water"},
        {"colour": "#2D7600", "label": "Pasteurised effluent"},
        {"colour": "#b46504", "label": "Filter effluent"},
        {"colour": "#B09500", "label": "Biogas"},
    ],
}
