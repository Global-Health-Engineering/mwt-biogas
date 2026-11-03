"""Keeps the persistent state in sync with the controller (spec sections 3, 9, 13).

- Unreadable file at start-up: FAULT_STORAGE (plant Level 3) until a
  commissioning user reinitialises storage.
- Save failure later: STATE_SAVE_FAILED (plant Level 3). The state stays in
  memory, saving is retried every cycle, starts and resets are inhibited,
  and the state is reported as saved only after a successful save.
"""

from pasteurizer.persistence import PersistentState, StorageError


class StateKeeper:
    def __init__(self, store, log):
        self.store = store
        self.log = log
        self.unreadable = False
        try:
            self.restored = store.load()
        except StorageError as error:
            self.restored = PersistentState()
            self.unreadable = True
            log.record("FAULT_STORAGE_UNREADABLE", error=str(error))
        self.save_failed = False
        self._saved: PersistentState | None = None if self.unreadable else self.restored

    @property
    def healthy(self) -> bool:
        """True when the current state is known to be stored."""
        return not self.unreadable and not self.save_failed

    @property
    def saved(self) -> bool:
        return self.healthy and self._saved is not None

    def sync(self, state: PersistentState, *, force=False):
        """Save if changed, or retry after a failure. Never raises."""
        if self.unreadable:
            return
        if not force and not self.save_failed and state == self._saved:
            return
        try:
            self.store.save(state)
        except Exception as error:
            if not self.save_failed:
                self.log.record("STATE_SAVE_FAILED", error=str(error))
            self.save_failed = True
            return
        if self.save_failed:
            self.log.record("STATE_SAVE_RECOVERED")
        self.save_failed = False
        self._saved = state

    def reinitialize(self, state: PersistentState) -> str | None:
        """Write a fresh file after an unreadable one; returns an error text."""
        try:
            self.store.save(state)
        except Exception as error:
            return str(error)
        self.unreadable = False
        self.save_failed = False
        self._saved = state
        return None
