import json

import pytest

from omarchy_cast.backends import creds
from omarchy_cast.backends.creds import (
    creds_path,
    ensure_extend_creds,
    extend_creds_path,
)
from omarchy_cast.core.session import EXTEND, MIRROR


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(creds, "default_creds_path", lambda: tmp_path / "doubletake.json")
    return tmp_path


def write_mirror_creds(tmp_path, with_token=True):
    data = {
        "AA:BB:CC:DD:EE:01": {
            "pairing_id": "abc",
            "ed25519_public": "pub",
            "ed25519_seed": "seed",
        }
    }
    if with_token:
        data["AA:BB:CC:DD:EE:01"]["restore_token"] = "tok-mirror"
    p = tmp_path / "doubletake.json"
    p.write_text(json.dumps(data))
    return p


def test_mirror_uses_doubletake_default():
    assert creds_path(MIRROR) is None


def test_extend_uses_its_own_file(isolated):
    write_mirror_creds(isolated)
    assert creds_path(EXTEND) == extend_creds_path()


def test_extend_creds_copy_the_pairing(isolated):
    write_mirror_creds(isolated)
    path = ensure_extend_creds()
    data = json.loads(path.read_text())
    assert data["AA:BB:CC:DD:EE:01"]["pairing_id"] == "abc"
    assert data["AA:BB:CC:DD:EE:01"]["ed25519_seed"] == "seed"


def test_extend_creds_drop_the_restore_token(isolated):
    """Copying it would restore the mirror's output and silently mirror."""
    write_mirror_creds(isolated)
    data = json.loads(ensure_extend_creds().read_text())
    assert "restore_token" not in data["AA:BB:CC:DD:EE:01"]


def test_existing_extend_creds_are_not_overwritten(isolated):
    """Otherwise every cast would discard the stored output selection."""
    write_mirror_creds(isolated)
    path = ensure_extend_creds()
    data = json.loads(path.read_text())
    data["AA:BB:CC:DD:EE:01"]["restore_token"] = "tok-extend"
    path.write_text(json.dumps(data))

    ensure_extend_creds()
    again = json.loads(path.read_text())
    assert again["AA:BB:CC:DD:EE:01"]["restore_token"] == "tok-extend"


def test_missing_mirror_creds_still_yields_a_usable_path(isolated):
    """First ever cast is an extend: there is nothing to copy, and pairing
    will simply happen in the extend file instead."""
    path = ensure_extend_creds()
    assert path == extend_creds_path()
    assert json.loads(path.read_text()) == {}


def test_corrupt_mirror_creds_do_not_propagate(isolated):
    (isolated / "doubletake.json").write_text("{not json")
    path = ensure_extend_creds()
    assert json.loads(path.read_text()) == {}


def test_extend_creds_are_private(isolated):
    write_mirror_creds(isolated)
    assert ensure_extend_creds().stat().st_mode & 0o077 == 0


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown mode"):
        creds_path("sideways")
