"""The graphical picker, whichever one this desktop provides.

Omarchy replaced walker with a Quickshell-based menu, so `walker --dmenu`
stopped existing and `omarchy-cast menu` died with a traceback -- the keybind
was simply broken after a system update. The launcher is not something to
hardcode.

Two backends, probed in order:

  omarchy-menu-select / omarchy-menu-input   Omarchy's own menu
  walker --dmenu                             earlier Omarchy, and other setups

They differ in more than name: walker reads its options from stdin, Omarchy's
takes them as arguments. Both return the chosen line on stdout, which is what
the caller wants.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

OMARCHY_SELECT = "omarchy-menu-select"
OMARCHY_INPUT = "omarchy-menu-input"
WALKER = "walker"


def backend() -> str | None:
    """Which picker to use, or None if the desktop provides neither."""
    if shutil.which(OMARCHY_SELECT):
        return "omarchy"
    if shutil.which(WALKER):
        return "walker"
    return None


def _run(argv: list[str], stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        log.warning("picker %s failed: %s", argv[0], exc)
        return ""
    return result.stdout


def pick(prompt: str, entries: list[str]) -> str:
    """Show `entries` and return the chosen one, or "" if cancelled."""
    which = backend()
    if which == "omarchy":
        # Options are arguments here, not stdin.
        return _run([OMARCHY_SELECT, prompt, *entries]).strip()
    if which == "walker":
        return _run([WALKER, "--dmenu", "-p", prompt], stdin="\n".join(entries)).strip()

    log.warning("no menu program found (%s or %s)", OMARCHY_SELECT, WALKER)
    return ""


def ask(prompt: str) -> str:
    """Prompt for free text -- an address to connect to directly."""
    which = backend()
    if which == "omarchy":
        return _run([OMARCHY_INPUT, prompt]).strip()
    if which == "walker":
        # walker's dmenu with no options doubles as a text prompt.
        return _run([WALKER, "--dmenu", "-p", prompt], stdin="").strip()

    log.warning("no menu program found for text input")
    return ""


def missing_message() -> str:
    return (
        f"no menu program found. omarchy-cast looks for {OMARCHY_SELECT} "
        f"(Omarchy) or {WALKER}. Install one, or use the CLI: "
        f"omarchy-cast list / omarchy-cast start <id>"
    )
