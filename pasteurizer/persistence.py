"""State that must survive restarts and power loss (spec sections 3, 9, 13).

Saved: latched Level 2/3 faults, the GUI STOP hold, an exhausted low-flow
retry allowance, and whether the watchdog shut the controller down. The file
is written atomically on every change. A missing file means a first start; an
unreadable or corrupt file is a plant-wide Level 3 (FAULT_STORAGE).
"""

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path


FORMAT_VERSION = 1


class StorageError(Exception):
    pass


@dataclass
class PersistentState:
    latched_faults: list[str] = field(default_factory=list)
    stop_hold: bool = False
    low_flow_retry_exhausted: bool = False
    watchdog_tripped: bool = False

    def to_json(self) -> str:
        return json.dumps({"version": FORMAT_VERSION, **asdict(self)},
                          sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "PersistentState":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise StorageError(f"Corrupt state file: {error}") from None
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            raise StorageError("State file has an unknown format")
        try:
            state = cls(
                latched_faults=list(data["latched_faults"]),
                stop_hold=data["stop_hold"],
                low_flow_retry_exhausted=data["low_flow_retry_exhausted"],
                watchdog_tripped=data["watchdog_tripped"],
            )
        except (KeyError, TypeError) as error:
            raise StorageError(f"State file incomplete: {error}") from None
        if not all(isinstance(code, str) for code in state.latched_faults) or not all(
            isinstance(value, bool) for value in
            (state.stop_hold, state.low_flow_retry_exhausted, state.watchdog_tripped)
        ):
            raise StorageError("State file has invalid values")
        return state


class FileStore:
    """JSON file, replaced atomically (write temp, fsync, rename)."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> PersistentState:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return PersistentState()
        except OSError as error:
            raise StorageError(f"Cannot read state file: {error}") from None
        return PersistentState.from_json(text)

    def save(self, state: PersistentState):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as file:
            file.write(state.to_json())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
        try:
            directory = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return  # Not supported on every platform (e.g. Windows).
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class MemoryStore:
    """Stand-in for simulation; `corrupt` simulates an unreadable file."""

    def __init__(self, state: PersistentState | None = None, corrupt=False):
        self.text = state.to_json() if state is not None else None
        self.corrupt = corrupt
        self.fail_saves = False         # simulates a storage write failure

    def load(self) -> PersistentState:
        if self.corrupt:
            raise StorageError("Simulated corrupt state file")
        if self.text is None:
            return PersistentState()
        return PersistentState.from_json(self.text)

    def save(self, state: PersistentState):
        if self.fail_saves:
            raise OSError("Simulated storage write failure")
        self.text = state.to_json()
        self.corrupt = False
