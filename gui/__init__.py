"""Read-only browser GUI for the mwt-biogas controller (separate process).

    link.py         polls the controller's localhost status publisher; staleness
    tags.py         drawing tags, readable names and software ids (confirmed)
    history_api.py  events, trends and CSV export from the historian (read-only)
    server.py       HTTP server: pages, static files and the JSON/CSV API
    static/         HTML, CSS and JavaScript of the main window and log window

The GUI never touches RevPi I/O, never writes the historian and has no plant
commands. Closing or crashing it cannot affect the controller.
"""
