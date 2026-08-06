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


class _SetupFailed(Exception):
    """Internal: the output exists but could not be made usable."""


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

    # Past this point the output exists, so every failure path -- including an
    # unexpected exception -- has to remove it again. Returning None while
    # leaving it behind used to strand a stray 1920x1080 monitor on the desktop
    # *and* tell the user no virtual display could be created: wrong on both
    # counts, and a direct violation of the spec's "no output left behind".
    #
    # Until the post-create read succeeds the real name is unknown, so failures
    # before then can only try the name that was requested.
    name = VIRTUAL_NAME
    try:
        after = _monitor_names(runner)
        if after is None:
            log.warning("could not list monitors after creating a virtual output")
            raise _SetupFailed

        new = sorted(after - before)
        if not new:
            log.warning("hyprctl reported success but no new output appeared")
            raise _SetupFailed

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
            # Created but not scaled correctly: unusable as a desktop.
            log.warning("could not configure geometry for virtual output %s", name)
            raise _SetupFailed
    except _SetupFailed:
        remove(name, runner)
        return None
    except Exception:
        log.warning("virtual output setup failed unexpectedly", exc_info=True)
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
    """Remove our own virtual output if a crash left it behind.

    Only VIRTUAL_NAME is removed. This used to sweep every `HEADLESS*` output
    as well, which destroys outputs omarchy-cast never created -- wayvnc and
    Sunshine both make them -- and it runs at daemon start and on every extend,
    so a user's live remote-desktop output vanished on any invocation.

    The cost is that an output Hyprland renamed to HEADLESS-N (create() warns
    when that happens) is no longer swept up after a crash. Leaving a stray of
    ours behind is recoverable; deleting someone else's live output is not.
    """
    if not available():
        return 0
    names = _monitor_names(runner)
    if not names:
        return 0
    removed = 0
    for name in sorted(names):
        if name == VIRTUAL_NAME:
            if remove(name, runner):
                removed += 1
        elif name.startswith("HEADLESS"):
            log.info(
                "leaving headless output %s alone; omarchy-cast did not create it",
                name,
            )
    return removed
