"""Choosing a menu program at runtime.

Omarchy replaced walker with a Quickshell menu. `omarchy-cast menu` hardcoded
`walker --dmenu`, so after a system update the cast keybind died with a
traceback -- the launcher is not something to hardcode.
"""

import pytest

from omarchy_cast.cli import picker


@pytest.fixture(autouse=True)
def real_picker(monkeypatch):
    """This module tests picker itself, so undo conftest's global stub."""
    import importlib
    importlib.reload(picker)
    yield


def only(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def record(calls):
    def run(argv, stdin=None):
        calls.append((argv, stdin))
        return "chosen\n"
    return run


def test_omarchy_menu_is_preferred(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", only("omarchy-menu-select", "walker"))
    assert picker.backend() == "omarchy"


def test_walker_is_the_fallback(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", only("walker"))
    assert picker.backend() == "walker"


def test_no_menu_at_all_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", only())
    assert picker.backend() is None


def test_omarchy_takes_options_as_arguments(monkeypatch):
    """The two programs differ in more than name: walker reads stdin, Omarchy's
    takes arguments. Sending entries the wrong way yields an empty menu."""
    calls = []
    monkeypatch.setattr(picker.shutil, "which", only("omarchy-menu-select"))
    monkeypatch.setattr(picker, "_run", record(calls))

    assert picker.pick("Cast to", ["Apple TV", "Meeting Room"]) == "chosen"

    argv, stdin = calls[0]
    assert argv == ["omarchy-menu-select", "Cast to", "Apple TV", "Meeting Room"]
    assert stdin is None


def test_walker_takes_options_on_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(picker.shutil, "which", only("walker"))
    monkeypatch.setattr(picker, "_run", record(calls))

    picker.pick("Cast to", ["Apple TV", "Meeting Room"])

    argv, stdin = calls[0]
    assert argv == ["walker", "--dmenu", "-p", "Cast to"]
    assert stdin == "Apple TV\nMeeting Room"


def test_text_input_uses_the_matching_program(monkeypatch):
    calls = []
    monkeypatch.setattr(picker.shutil, "which", only("omarchy-menu-select", "omarchy-menu-input"))
    monkeypatch.setattr(picker, "_run", record(calls))

    picker.ask("Receiver IP address")

    assert calls[0][0] == ["omarchy-menu-input", "Receiver IP address"]


def test_a_missing_menu_returns_empty_rather_than_raising(monkeypatch):
    """The keybind died with a traceback when walker vanished. Never again."""
    monkeypatch.setattr(picker.shutil, "which", only())

    assert picker.pick("Cast to", ["a"]) == ""
    assert picker.ask("Address") == ""


def test_a_menu_that_fails_to_launch_does_not_raise(monkeypatch):
    monkeypatch.setattr(picker.shutil, "which", only("walker"))

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(picker.subprocess, "run", boom)

    assert picker.pick("Cast to", ["a"]) == ""
