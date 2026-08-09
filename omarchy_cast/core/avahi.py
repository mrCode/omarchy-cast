"""Discovery through avahi instead of a second mDNS stack of our own.

Measured on a real network: a freshly started python-zeroconf browser took
15.7s to produce its FIRST result and found one receiver in ninety seconds,
while `avahi-browse` listed six instantly. Avahi is not faster at querying --
it has simply been running since boot, with a warm cache and long-lived
subscriptions it keeps refreshed. Our daemon cannot compete with that by
starting its own browser, because the daemon exits when idle and starts cold
almost every time.

So we ask the machine's mDNS resolver rather than being a second one. That
also means omarchy-cast sees exactly what the rest of the desktop sees, which
is what the user expects when `avahi-browse` shows a receiver and we do not.

`avahi-browse -rtp` is used rather than the D-Bus API deliberately: the parse
is a dozen lines against a stable, documented format, where the D-Bus resolver
protocol is a great deal of code for the same answer.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import threading

from omarchy_cast.core.device import Device

log = logging.getLogger(__name__)

AIRPLAY_TYPE = "_airplay._tcp"
CAST_TYPE = "_googlecast._tcp"

# avahi-browse blocks until it has dumped the cache; this only bounds a hang.
BROWSE_TIMEOUT = 20.0

_ESCAPE = re.compile(r"\\(\d{3})")


def unescape(name: str) -> str:
    """avahi escapes bytes as \\NNN decimal -- spaces, quotes, any UTF-8.

    Feeding the raw form onward produces device names like
    `Khalid\\226\\128\\153s\\032MacBook` in the menu.
    """
    out = bytearray()
    i = 0
    while i < len(name):
        m = _ESCAPE.match(name, i)
        if m:
            out.append(int(m.group(1)))
            i = m.end()
        else:
            out.extend(name[i].encode())
            i += 1
    return out.decode("utf-8", errors="replace")


def available(runner=None) -> bool:
    runner = runner or _run
    code, _ = runner(["avahi-browse", "--version"])
    return code == 0


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=BROWSE_TIMEOUT
        )
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return 1, ""
    return proc.returncode, proc.stdout


def parse(output: str, protocol: str) -> list[Device]:
    """Parse `avahi-browse -rtp` resolved records into Devices.

    Resolved lines start with '=' and are semicolon separated:
        =;iface;proto;name;type;domain;host;address;port;txt

    The `proto` field is the mDNS TRANSPORT, not the address family: real
    output says IPv6 while carrying `10.10.10.231`. Filtering on it drops
    everything. What matters is that the ADDRESS is v4, since a link-local v6
    address is not a usable cast target -- so the address itself is validated.
    """
    devices: dict[str, Device] = {}
    for line in output.splitlines():
        if not line.startswith("="):
            continue
        f = line.split(";")
        if len(f) < 10:
            continue
        name, address, port, txt = unescape(f[3]), f[7], f[8], f[9]
        if not port.isdigit() or not _is_ipv4(address):
            continue

        unique = _txt_value(txt, "deviceid" if protocol == "airplay" else "id")
        model = _txt_value(txt, "model" if protocol == "airplay" else "md")
        friendly = _txt_value(txt, "fn") if protocol == "cast" else None

        device = Device(
            id=Device.make_id(protocol, unique or address),
            name=friendly or name,
            address=address,
            port=int(port),
            protocol=protocol,
            model=model,
        )
        devices[device.id] = device
    return list(devices.values())


def _is_ipv4(address: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
    except ValueError:
        return False


def _txt_value(txt: str, key: str) -> str | None:
    """Pull one key from avahi's quoted TXT list: "a=1" "b=2"."""
    for entry in re.findall(r'"([^"]*)"', txt):
        if entry.startswith(f"{key}="):
            return unescape(entry[len(key) + 1:]) or None
    return None


def browse(protocol: str, runner=None) -> list[Device]:
    runner = runner or _run
    service = AIRPLAY_TYPE if protocol == "airplay" else CAST_TYPE
    code, out = runner(["avahi-browse", "-rtp", service])
    if code != 0:
        log.debug("avahi-browse %s exited %s", service, code)
        return []
    return parse(out, protocol)


class AvahiDiscovery:
    """Same surface as core.discovery.Discovery, backed by avahi's cache."""

    def __init__(self, runner=None) -> None:
        self._runner = runner or _run
        self._manual: dict[str, Device] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        """Nothing to start: avahi is already running and already knows."""

    def stop(self) -> None:
        pass

    def _discovered(self) -> list[Device]:
        found = []
        for protocol in ("airplay", "cast"):
            found.extend(browse(protocol, self._runner))
        return found

    def devices(self) -> list[Device]:
        discovered = self._discovered()
        seen = {(d.protocol, d.address) for d in discovered}
        with self._lock:
            # Same rule as the zeroconf path: a manual entry is hidden, never
            # deleted, once the same receiver is discovered.
            extra = [
                d for d in self._manual.values()
                if (d.protocol, d.address) not in seen
            ]
        return sorted(discovered + extra, key=lambda d: (d.protocol, d.name))

    def has_discovered(self) -> bool:
        return bool(self._discovered())

    def add(self, device: Device) -> None:
        with self._lock:
            self._manual[device.id] = device

    def remove(self, device_id: str) -> bool:
        with self._lock:
            return self._manual.pop(device_id, None) is not None
