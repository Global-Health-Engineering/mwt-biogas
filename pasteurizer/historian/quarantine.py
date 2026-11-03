"""Corrupt historian databases: detect, preserve, report (spec section 13).

A corrupt database is never repaired or deleted. It is renamed, together
with its -wal and -shm files, to

    historian.corrupt-<UTC>.sqlite      (then -2, -3, ... if that name exists)

so an earlier preserved file is never overwritten, and a fresh database is
started. Preserved files do not count against `max_database_mb`; their disk
usage is reported separately. Remove them by hand after analysis.
"""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

SIDECARS = ("", "-wal", "-shm")
_MARKERS = ("not a database", "malformed", "corrupt")


def is_corruption(error) -> bool:
    """True for errors that retrying cannot fix (not e.g. disk full or I/O)."""
    return (isinstance(error, sqlite3.DatabaseError)
            and any(marker in str(error).lower() for marker in _MARKERS))


def _target(path: Path, stamp: str, number: int) -> Path:
    suffix = "" if number == 1 else f"-{number}"
    return path.with_name(f"{path.stem}.corrupt-{stamp}{suffix}{path.suffix}")


def preserve(path, wall_epoch: float) -> Path:
    """Rename the database and its sidecars to a name not used before."""
    path = Path(path)
    stamp = datetime.fromtimestamp(wall_epoch, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    number = 1
    while any(Path(f"{_target(path, stamp, number)}{s}").exists() for s in SIDECARS):
        number += 1
    target = _target(path, stamp, number)
    for sidecar in SIDECARS:
        source = Path(f"{path}{sidecar}")
        if source.exists():
            source.rename(f"{target}{sidecar}")     # target checked: never overwrites
    return target


def _size(path: Path) -> int:
    return sum(Path(f"{path}{s}").stat().st_size for s in SIDECARS
               if Path(f"{path}{s}").exists())


def storage_report(path) -> dict:
    """Disk usage of the live database and, separately, of preserved corrupt files."""
    path = Path(path)
    corrupt = sorted(p for p in path.parent.glob(f"{path.stem}.corrupt-*{path.suffix}"))
    return {
        "database_mb": round(_size(path) / 1048576, 3),
        "corrupt_files": [p.name for p in corrupt],
        "corrupt_mb": round(sum(_size(p) for p in corrupt) / 1048576, 3),
    }
