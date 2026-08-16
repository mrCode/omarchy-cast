"""Suite-wide guards against tests touching the real machine.

This project has twice shipped tests that reached out of the sandbox: once
switching the developer's actual monitor mode, and once writing a fake receiver
into the user's real `manual-devices.json`, where it would have appeared in
their cast menu. Both passed review, because a test that quietly does the wrong
thing to your machine still goes green.

Anything that resolves a path from the environment gets redirected here, for
every test, without each test having to remember.
"""

import pytest


@pytest.fixture(autouse=True)
def no_real_menus(monkeypatch):
    """Never let a test open the desktop's menu.

    A test called the real `omarchy-menu-input`, which put a dialog on the
    developer's screen and hung the suite until it was killed by hand. Tests
    that exercise the menu must stub these explicitly; anything else fails
    loudly instead of blocking.
    """
    from omarchy_cast.cli import picker

    def forbidden(*a, **k):
        raise AssertionError(
            "a test tried to open the real desktop menu; stub picker.pick/ask"
        )

    monkeypatch.setattr(picker, "pick", forbidden)
    monkeypatch.setattr(picker, "ask", forbidden)
    monkeypatch.setattr(picker, "backend", lambda: "stub")


@pytest.fixture(autouse=True)
def isolate_user_state(tmp_path, monkeypatch):
    """Point every XDG lookup at a per-test temp directory.

    autouse and unconditional: an opt-in guard only protects the tests whose
    author already knew they needed it, which is never the ones that bite.
    """
    for var in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        d = tmp_path / var.lower()
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(d))

    # HOME is the fallback when the XDG vars are unset, so a module computing
    # `Path.home() / ".local" / "state"` directly would still escape.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    yield
