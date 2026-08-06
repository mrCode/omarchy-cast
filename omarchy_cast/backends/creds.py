"""Which credentials file doubletake gets, per cast mode.

doubletake stores one `restore_token` per device, which is its portal output
selection. Mirror and extend need different outputs, so they get different
files via doubletake's `-creds` flag. That avoids editing doubletake's own
store or depending on its JSON layout beyond removing one key from our copy.
"""

import json
import logging
from pathlib import Path

from omarchy_cast.core.display import state_dir
from omarchy_cast.core.session import EXTEND, MIRROR

log = logging.getLogger(__name__)


def default_creds_path() -> Path:
    return Path.home() / ".config" / "doubletake" / "credentials.json"


def extend_creds_path() -> Path:
    return state_dir() / "doubletake-extend-credentials.json"


def ensure_extend_creds() -> Path:
    """Create the extend credentials file if absent, and return its path.

    The pairing is copied so extend does not need a second PIN. The restore
    token is dropped: keeping it would restore the mirror's output selection
    and silently mirror instead of extending.
    """
    path = extend_creds_path()
    if path.exists():
        return path

    data: dict = {}
    source = default_creds_path()
    if source.exists():
        try:
            data = json.loads(source.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("could not read mirror credentials (%s); starting fresh", exc)
            data = {}

    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict):
                entry.pop("restore_token", None)
    else:
        data = {}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    log.info("created extend credentials at %s", path)
    return path


def creds_path(mode: str) -> Path | None:
    """None means: let doubletake use its own default file."""
    if mode == MIRROR:
        return None
    if mode == EXTEND:
        return ensure_extend_creds()
    raise ValueError(f"unknown mode: {mode}")
