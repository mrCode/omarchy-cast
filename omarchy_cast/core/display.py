"""Temporary display-mode switching for AirPlay.

doubletake negotiates the AirPlay stream at 1920x1080 and its software capture
path has no scaler, so on a higher-resolution display it sends a mismatched SPS
and the receiver drops the connection. Until that is fixed upstream
(doubletake#28) the display has to match while casting.

The previous mode is written to disk before switching, so a crash or a killed
daemon cannot leave the user stranded at the wrong resolution -- the next run
restores it.
"""

import json
import logging
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

STREAM_WIDTH = 1920
STREAM_HEIGHT = 1080


@dataclass(frozen=True)
class MonitorMode:
    name: str
    width: int
    height: int
    refresh: float
    x: int
    y: int
    scale: float

    def as_hyprctl(self) -> str:
        return (
            f"{self.name},{self.width}x{self.height}@{self.refresh:.5f},"
            f"{self.x}x{self.y},{self.scale}"
        )

    @property
    def matches_stream(self) -> bool:
        return self.width == STREAM_WIDTH and self.height == STREAM_HEIGHT


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "omarchy-cast"


def saved_mode_path() -> Path:
    return state_dir() / "display-before-cast.json"


def _run(argv: list[str]) -> tuple[int, str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout


def available() -> bool:
    return shutil.which("hyprctl") is not None


def focused_monitor(runner=_run) -> MonitorMode | None:
    """The monitor the portal is most likely capturing.

    Hyprland does not tell us which output the portal picked, so we take the
    focused one. Documented rather than guessed at silently.
    """
    code, out = runner(["hyprctl", "-j", "monitors"])
    if code != 0:
        return None
    try:
        monitors = json.loads(out)
    except json.JSONDecodeError:
        log.debug("could not parse hyprctl monitors output")
        return None

    if not monitors:
        return None
    chosen = next((m for m in monitors if m.get("focused")), monitors[0])
    return MonitorMode(
        name=chosen["name"],
        width=int(chosen["width"]),
        height=int(chosen["height"]),
        refresh=float(chosen["refreshRate"]),
        x=int(chosen["x"]),
        y=int(chosen["y"]),
        scale=float(chosen["scale"]),
    )


def save_mode(mode: MonitorMode) -> None:
    path = saved_mode_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(mode)))


def load_saved_mode() -> MonitorMode | None:
    path = saved_mode_path()
    if not path.exists():
        return None
    try:
        return MonitorMode(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError) as exc:
        log.debug("discarding unreadable saved display mode: %s", exc)
        path.unlink(missing_ok=True)
        return None


def clear_saved_mode() -> None:
    saved_mode_path().unlink(missing_ok=True)


def apply_stream_mode(runner=_run) -> MonitorMode | None:
    """Switch the focused monitor to the streaming resolution.

    Returns the previous mode, or None if nothing was changed.
    """
    if not available():
        log.debug("hyprctl unavailable; leaving display alone")
        return None

    current = focused_monitor(runner)
    if current is None:
        return None
    if current.matches_stream:
        log.debug("display already %dx%d", STREAM_WIDTH, STREAM_HEIGHT)
        return None

    target = MonitorMode(
        name=current.name,
        width=STREAM_WIDTH,
        height=STREAM_HEIGHT,
        refresh=60.0,
        x=current.x,
        y=current.y,
        scale=1.0,
    )
    # Saved before switching: if this process dies, the next run restores it.
    save_mode(current)
    code, _ = runner(["hyprctl", "keyword", "monitor", target.as_hyprctl()])
    if code != 0:
        log.warning("could not switch %s to %dx%d", current.name, STREAM_WIDTH, STREAM_HEIGHT)
        clear_saved_mode()
        return None

    log.info(
        "switched %s to %dx%d for casting (was %dx%d@%.0f scale %.2f)",
        current.name, STREAM_WIDTH, STREAM_HEIGHT,
        current.width, current.height, current.refresh, current.scale,
    )
    return current


def restore_mode(runner=_run) -> bool:
    """Restore the mode saved by apply_stream_mode. Safe to call any time."""
    saved = load_saved_mode()
    if saved is None:
        return False
    if not available():
        return False

    code, _ = runner(["hyprctl", "keyword", "monitor", saved.as_hyprctl()])
    if code != 0:
        # Keep the file: a later attempt can still put things right.
        log.warning("could not restore %s to %dx%d", saved.name, saved.width, saved.height)
        return False

    clear_saved_mode()
    log.info("restored %s to %dx%d", saved.name, saved.width, saved.height)
    return True
