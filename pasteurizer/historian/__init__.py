"""Historian: persistent event log and periodic process data (spec section 13).

    record_queue.py   bounded hand-over queue (control thread -> writer)
    writer.py         writer and its background thread, retry, retention
    service.py        Historian facade used by the event log
    sqlite_store.py   local SQLite database (events, samples, boots)
    quarantine.py     corrupt databases: preserve, never overwrite, report
    memory_store.py   in-memory store for simulation and tests
    csv_export.py     read-only CSV export

Storage never runs inside the control scan. A historian problem is only ever
a yellow warning: LOGGER_FAILURE (self-clearing) or, after a corrupt database
was preserved and replaced, HISTORIAN_DB_RECOVERED (cleared by acknowledging
it; the preserved files are kept). Both are completely separate from the
controller-state file (persistence.py, STATE_SAVE_FAILED).
"""

from pathlib import Path

from pasteurizer.historian.memory_store import MemoryHistorianStore
from pasteurizer.historian.service import Historian
from pasteurizer.historian.sqlite_store import SqliteStore

DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / "logs" / "historian.sqlite"


def open_sqlite_historian(settings, path=DEFAULT_DATABASE, **kwargs) -> Historian:
    return Historian(SqliteStore(path, settings), settings, **kwargs)


__all__ = ["DEFAULT_DATABASE", "Historian", "MemoryHistorianStore", "SqliteStore",
           "open_sqlite_historian"]
