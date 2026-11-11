"""One controller on the hardware at a time (an OS lock, released even after a crash)."""

import os
from pathlib import Path


class RunLock:
    """Held by the controller runner (and checked by safe_outputs.py).

    Linux: flock on the whole file. Windows: msvcrt.locking locks bytes from
    the current file position, so the lock is always taken on byte 0 (never
    at the end of the file, where every runner would lock a different byte).
    """

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Read/write without append mode: seek(0) also applies to writes.
        self.file = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:                     # held by another runner
            self.file.close()
            self.file = None
            return False
        # Only the holder writes (its process id, for diagnosis).
        self.file.seek(0)
        self.file.truncate()
        self.file.write(f"{os.getpid()}\n")
        self.file.flush()
        return True

    def release(self):
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass                            # closing the file releases it anyway
        finally:
            self.file.close()
            self.file = None
