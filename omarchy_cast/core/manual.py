"""Devices the user registered by address, remembered across daemon restarts.

mDNS is not always usable. An access point can filter multicast per device, so
a receiver stays perfectly reachable while never answering discovery -- on one
tested network an Apple TV served AirPlay on port 7000 and answered no mDNS
query at all, while a MacBook on the same subnet and access point answered
both. `--address` exists for exactly that.

Without this module that escape hatch barely worked: the daemon exits after 30s
idle, taking its in-memory device list with it, so a manually added receiver
vanished within a minute and the address had to be retyped for every cast. The
receivers that need `--address` are precisely the ones that need it *every
time*, since discovery will never start finding them on its own.

The file is a plain JSON list, safe to delete or hand-edit; a corrupt or
unreadable one is logged and ignored rather than taking the daemon down over a
convenience feature.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

from omarchy_cast.core.device import PROTOCOLS, Device

log = logging.getLogger(__name__)

FILENAME = "manual-devices.json"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "omarchy-cast"


def path() -> Path:
    return state_dir() / FILENAME


def _as_device(raw: dict) -> Device | None:
    """Build a Device, or None if the entry is unusable.

    Entries are validated one at a time so a single bad record -- a
    hand-edit, a protocol removed in a later version -- costs only itself
    rather than every remembered device.
    """
    try:
        if raw.get("protocol") not in PROTOCOLS:
            raise ValueError(f"unknown protocol: {raw.get('protocol')!r}")
        return Device(
            id=str(raw["id"]),
            name=str(raw["name"]),
            address=str(raw["address"]),
            port=int(raw["port"]),
            protocol=str(raw["protocol"]),
            model=raw.get("model"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("ignoring unusable remembered device %r: %s", raw, exc)
        return None


def load(file: Path | None = None) -> list[Device]:
    file = file or path()
    try:
        raw = json.loads(file.read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s; ignoring remembered devices", file, exc)
        return []

    if not isinstance(raw, list):
        log.warning("%s is not a list; ignoring remembered devices", file)
        return []

    return [d for d in (_as_device(r) for r in raw if isinstance(r, dict)) if d]


def save(devices: list[Device], file: Path | None = None) -> bool:
    """Write the list atomically. Returns False instead of raising.

    Atomic because the daemon can be killed at any moment -- logout, reboot,
    a crash mid-cast -- and a half-written file would lose every remembered
    device, not just the one being added.
    """
    file = file or path()
    payload = json.dumps(
        [
            {
                "id": d.id,
                "name": d.name,
                "address": d.address,
                "port": d.port,
                "protocol": d.protocol,
                "model": d.model,
            }
            for d in devices
        ],
        indent=2,
    )

    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(file.parent), prefix=f".{FILENAME}.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload + "\n")
            os.replace(tmp, file)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        log.warning("could not save remembered devices to %s: %s", file, exc)
        return False

    return True


def remember(device: Device, file: Path | None = None) -> bool:
    """Add or update a device, keyed by id."""
    devices = [d for d in load(file) if d.id != device.id]
    devices.append(device)
    return save(devices, file)


def forget(device_id: str, file: Path | None = None) -> bool:
    """Drop a device. Returns whether anything was actually removed.

    Needed because these entries never expire on their own: an address that
    was right on one network is wrong on the next, and without this the menu
    would accumulate receivers that can never be reached.
    """
    devices = load(file)
    kept = [d for d in devices if d.id != device_id]
    if len(kept) == len(devices):
        return False
    save(kept, file)
    return True

