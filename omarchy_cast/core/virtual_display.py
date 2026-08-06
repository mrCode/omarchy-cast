"""Hyprland virtual outputs, used for extend mode.

The only module that runs `hyprctl output`. Backends go through it, the same
way `core/display.py` isolates display-mode switching, so the whole test suite
can run without a compositor.
"""

import json
import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

VIRTUAL_NAME = "omarchy-cast"
MODE_LINE = "1920x1080@60"

# A virtual output defaults to scale 2.0 -- a logical 960x540, useless as a
# desktop. `auto` places it to the right of existing outputs.
CONFIG = "{name}," + MODE_LINE + ",auto,1"


def _run(argv: list[str]) -> tuple[int, str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout


def available() -> bool:
    return shutil.which("hyprctl") is not None


def _monitor_names(runner) -> set[str] | None:
    code, out = runner(["hyprctl", "-j", "monitors"])
    if code != 0:
        return None
    try:
        return {m["name"] for m in json.loads(out)}
    except (json.JSONDecodeError, TypeError, KeyError):
        log.debug("could not parse hyprctl monitors output")
        return None


def _is_virtual(name: str) -> bool:
    return name == VIRTUAL_NAME or name.startswith("HEADLESS")


def create(runner=_run) -> str | None:
    """Create the virtual output and return the name Hyprland actually used."""
    if not available():
        log.debug("hyprctl unavailable; cannot create a virtual output")
        return None

    before = _monitor_names(runner)
    if before is None:
        return None

    code, _ = runner(["hyprctl", "output", "create", "headless", VIRTUAL_NAME])
    if code != 0:
        log.warning("could not create a virtual output")
        return None

    after = _monitor_names(runner)
    if after is None:
        return None

    new = sorted(after - before)
    if not new:
        log.warning("hyprctl reported success but no new output appeared")
        return None

    name = new[0]
    if name != VIRTUAL_NAME:
        # Naming is undocumented; if a Hyprland version drops it the name
        # changes every run and the portal restore token breaks each time.
        log.warning(
            "requested output name %r but got %r; the portal will re-prompt "
            "on every cast", VIRTUAL_NAME, name,
        )

    code, _ = runner(["hyprctl", "keyword", "monitor", CONFIG.format(name=name)])
    if code != 0:
        # The output is half-configured: created but not scaled correctly. Remove
        # it rather than leave a stray output on the user's desktop.
        log.warning("could not configure geometry for virtual output %s", name)
        remove(name, runner)
        return None

    log.info("created virtual output %s at %s", name, MODE_LINE)
    return name


def remove(name: str, runner=_run) -> bool:
    if not available():
        return False
    code, _ = runner(["hyprctl", "output", "remove", name])
    if code != 0:
        log.warning("could not remove virtual output %s", name)
        return False
    log.info("removed virtual output %s", name)
    return True


def cleanup_strays(runner=_run) -> int:
    """Remove virtual outputs left behind by a crash. Called at daemon start."""
    if not available():
        return 0
    names = _monitor_names(runner)
    if not names:
        return 0
    removed = 0
    for name in sorted(names):
        if _is_virtual(name) and remove(name, runner):
            removed += 1
    return removed
