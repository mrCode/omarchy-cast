"""Small network facts used to explain failures accurately.

AirPlay's failure message used to blame the firewall outright. That was right
often enough to be dangerous: when the laptop sat on 172.26.x and the Apple TV
on 10.10.10.x, the firewall was wide open, logged nothing, and the real problem
was that the receiver's connection back to us never crossed the subnet
boundary. A confident wrong explanation costs more than a vague one.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return 1, ""
    return proc.returncode, proc.stdout


def routed_via_gateway(address: str, runner=_run) -> bool | None:
    """Is `address` reached through a gateway rather than being on-link?

    True means a different subnet, which for AirPlay means the receiver very
    likely cannot open its return connection to us. None means we could not
    tell -- callers must not present a guess as a fact.
    """
    code, out = runner(["ip", "route", "get", address])
    if code != 0 or not out.strip():
        return None

    first = out.splitlines()[0]
    # `ip route get` prints "... via <gw> dev ..." only when off-link.
    return " via " in first
