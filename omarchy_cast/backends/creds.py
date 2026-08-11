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


def mirror_creds_path() -> Path:
    """Mirror captures a virtual output too, so it needs its own token.

    Mirror used to capture the panel directly, so doubletake's default
    credentials were right. Now that it captures a mirrored virtual output,
    reusing them would replay a restore token pointing at the real panel --
    and silently capture that instead, at whatever resolution it happens to
    be, which is the failure the 1080p switch existed to avoid.
    """
    return state_dir() / "doubletake-mirror-credentials.json"


def _derive_creds(path: Path, label: str) -> Path:
    """Copy the pairing from doubletake's own store, minus the restore token.

    Pairing is preserved so no second PIN is needed; the token is dropped so
    the portal asks which output to share.
    """
    if path.exists():
        return path

    data: dict = {}
    source = default_creds_path()
    if source.exists():
        try:
            data = json.loads(source.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("could not read default credentials (%s); starting fresh", exc)
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
    log.info("created %s credentials at %s", label, path)
    return path


def ensure_mirror_creds() -> Path:
    return _derive_creds(mirror_creds_path(), "mirror")


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


def creds_path(mode: str, virtual: bool = False) -> Path | None:
    """Which credentials file this cast should use.

    None means doubletake's own default, which is correct only when capturing
    the real panel. `virtual=True` means the cast captures a virtual output, so
    it needs a token of its own -- replaying the default one would select the
    panel instead, silently.
    """
    if mode == MIRROR:
        return ensure_mirror_creds() if virtual else None
    if mode == EXTEND:
        return ensure_extend_creds()
    raise ValueError(f"unknown mode: {mode}")
