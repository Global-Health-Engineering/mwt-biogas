"""Browser GUI for the mwt-biogas controller (separate process).

    link.py         polls the controller's localhost status publisher; staleness
    tags.py         drawing tags, readable names and software ids (confirmed)
    history_api.py  events, trends and CSV export from the historian (read-only)
    auth.py         users file (PBKDF2 hashes), sessions, login lockout
    server.py       HTTP server: pages, static files, JSON/CSV API, login and
                    command routes (commands go to the controller's command server)
    static/         HTML, CSS and JavaScript: main window, log window, manual panel

The GUI never touches RevPi I/O and never writes the historian. Without a
login it is read-only; logged-in commands are checked again and logged by the
controller. Closing or crashing the GUI cannot switch anything on: manual
requests expire in the controller unless the page renews them.
"""
