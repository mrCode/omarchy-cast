"""Desktop notifications, shared by the daemon and the CLI.

Both used to have their own copy of this, and both sent every message at
critical urgency. On mako a critical notification never expires, so an
afternoon of casting left a column of banners the user had to click away one
by one -- and a single failed start produced two of them, because the daemon
notified the FAILED transition while the CLI separately notified the error
that the very same failure returned to it.

Two rules keep that from happening again:

- Only something the user must act on is urgent. Urgent means sticky, and
  sticky is a cost paid by the user, not by us.
- Every notification carries mako's synchronous hint, so a new one replaces
  the previous instead of stacking. Casting is a single ongoing activity;
  its status should occupy one banner, not a queue.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

APP = "omarchy-cast"

# mako (and swaync) replace rather than stack when this hint matches, so
# repeated status messages reuse one banner. Unknown to other daemons, which
# ignore it harmlessly.
_SYNCHRONOUS = f"string:x-canonical-private-synchronous:{APP}"

# Long enough to read a device name, short enough not to sit in the corner.
EXPIRE_MS = 5000


def notify(message: str, *, urgent: bool = False) -> None:
    """Show a desktop notification. Best effort; never raises.

    `urgent` is for failures the user is not already watching for -- a cast
    that died mid-stream. It is deliberately sticky. Errors returned straight
    to a command the user just ran are not urgent: they already have the
    user's attention.
    """
    argv = ["notify-send", "-a", APP, "-h", _SYNCHRONOUS]
    if urgent:
        argv += ["-u", "critical"]
    else:
        argv += ["-u", "normal", "-t", str(EXPIRE_MS)]

    try:
        subprocess.run([*argv, APP, message], check=False)
    except OSError:
        log.debug("notify-send unavailable", exc_info=True)
