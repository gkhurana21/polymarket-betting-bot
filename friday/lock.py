"""Single-instance lock.

Three `--with-loop` engines were once left running concurrently for 1.7 days,
all writing the same database. In dry-run that only inflated the cycle count.
Live it would be three independent quoting engines competing for the same
markets, placing and cancelling each other's orders, each blind to the others'
inventory — every position limit in the system silently multiplied by three.

An advisory flock is enough: it is released automatically if the process dies,
so a crash cannot leave a stale lock behind the way a bare PID file does.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from friday.logger import get_logger

_log = get_logger(__name__)


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """Exclusive advisory lock on a file. Use as a context manager."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def acquire(self) -> "InstanceLock":
        self._fh = self._path.open("a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.seek(0)
            holder = self._fh.read().strip() or "unknown"
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(
                f"another Friday engine holds {self._path} (pid {holder}). "
                f"Only one quoting loop may run — concurrent engines multiply "
                f"every position limit."
            )
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        _log.info("instance lock acquired (pid %d)", os.getpid())
        return self

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()
        return False
