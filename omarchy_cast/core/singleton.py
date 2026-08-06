"""One daemon at a time.

Nothing used to enforce this. `serve()` unlinks an existing socket and rebinds,
so a second daemon simply took over, and the client spawns one whenever the
socket is momentarily absent -- during another daemon's idle shutdown, or when
two commands race. Three were observed running at once.

That was not merely untidy. Daemon startup sweeps leftover `omarchy-cast`
virtual outputs, which is right after a crash and catastrophic otherwise: a
second daemon cannot tell a leftover from the output of a cast that is running
right now, in a different process. It removed the live one, and the cast died
mid-stream with nothing in its own daemon's log to explain it -- the removal
had happened somewhere else entirely.

The lock is taken before the sweep and before the socket is touched, so a
second daemon exits before it can do any damage.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

FILENAME = "daemon.lock"

# How long to keep trying before concluding another daemon really is alive.
# Covers the window where a daemon is shutting down but has not yet dropped
# its lock; without it a client spawning a replacement in that window would
# get no daemon at all.
DEFAULT_WAIT = 3.0
POLL = 0.1


def path(state_dir: Path | None = None) -> Path:
    if state_dir is None:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        state_dir = Path(base) / "omarchy-cast"
    return state_dir / FILENAME


def acquire(lock_path: Path | str | None = None, wait: float = DEFAULT_WAIT) -> IO | None:
    """Take the daemon lock, or return None if another daemon holds it.

    The returned file object must be kept alive for as long as the daemon
    runs: the lock belongs to the open file description, so letting it be
    garbage collected would silently release it. Closing it, or the process
    exiting for any reason including a crash, releases it -- so a daemon that
    dies badly cannot lock out its successors.
    """
    lock_path = Path(lock_path) if lock_path is not None else path()

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w")
    except OSError as exc:
        # Being unable to create a lock file must not stop the user casting.
        # Losing single-instance protection is the lesser failure.
        log.warning("could not open %s (%s); starting without the lock", lock_path, exc)
        return _DUMMY

    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(POLL)


class _Dummy:
    """Stands in for a lock we could not create, so callers have one code path.

    Deliberately not a real lock: it grants nothing and protects nothing. Used
    only when the filesystem refuses, where the alternative is refusing to cast.
    """

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_DUMMY = _Dummy()
