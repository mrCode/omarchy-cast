"""Display-mode switching for AirPlay.

The receiver drops the connection unless the display matches the resolution
doubletake negotiates, so the app switches it while casting and puts it back
afterwards -- including after a crash.
"""

import json

import pytest

from omarchy_cast.core import display
from omarchy_cast.core.display import (
    STREAM_HEIGHT,
    STREAM_WIDTH,
    MonitorMode,
    apply_stream_mode,
    clear_saved_mode,
    focused_monitor,
    load_saved_mode,
    restore_mode,
    save_mode,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(display, "available", lambda: True)


def monitors_json(width=2560, height=1600, refresh=240.0, scale=1.6, name="eDP-2", focused=True):
    return json.dumps([{
        "name": name, "width": width, "height": height, "refreshRate": refresh,
        "x": 0, "y": 0, "scale": scale, "focused": focused,
    }])


class FakeRunner:
    def __init__(self, monitors=None, fail_on=None):
        self.calls = []
        self._monitors = monitors if monitors is not None else monitors_json()
        self._fail_on = fail_on

    def __call__(self, argv):
        self.calls.append(argv)
        if self._fail_on and self._fail_on in " ".join(argv):
            return 1, ""
        if "monitors" in argv:
            return 0, self._monitors
        return 0, ""


def test_reads_the_focused_monitor():
    m = focused_monitor(FakeRunner())
    assert (m.name, m.width, m.height, m.scale) == ("eDP-2", 2560, 1600, 1.6)


def test_picks_the_focused_one_when_several():
    two = json.dumps([
        {"name": "HDMI-A-1", "width": 1920, "height": 1080, "refreshRate": 60.0,
         "x": 0, "y": 0, "scale": 1.0, "focused": False},
        {"name": "eDP-2", "width": 2560, "height": 1600, "refreshRate": 240.0,
         "x": 1920, "y": 0, "scale": 1.6, "focused": True},
    ])
    assert focused_monitor(FakeRunner(two)).name == "eDP-2"


def test_switches_and_returns_the_previous_mode():
    runner = FakeRunner()
    previous = apply_stream_mode(runner)
    assert (previous.width, previous.height) == (2560, 1600)
    switch = [c for c in runner.calls if "keyword" in c][0]
    assert f"{STREAM_WIDTH}x{STREAM_HEIGHT}" in switch[-1]
    assert switch[-1].startswith("eDP-2,")


def test_saves_the_previous_mode_before_switching():
    """Written to disk first, so a crash mid-cast is still recoverable."""
    apply_stream_mode(FakeRunner())
    saved = load_saved_mode()
    assert (saved.width, saved.height, saved.scale) == (2560, 1600, 1.6)


def test_no_change_when_already_at_stream_resolution():
    runner = FakeRunner(monitors_json(width=1920, height=1080, scale=1.0))
    assert apply_stream_mode(runner) is None
    assert not [c for c in runner.calls if "keyword" in c]
    assert load_saved_mode() is None


def test_restore_puts_the_exact_mode_back():
    runner = FakeRunner()
    apply_stream_mode(runner)
    assert restore_mode(runner) is True
    restored = [c for c in runner.calls if "keyword" in c][-1][-1]
    assert restored == "eDP-2,2560x1600@240.00000,0x0,1.6"


def test_restore_clears_the_saved_state():
    runner = FakeRunner()
    apply_stream_mode(runner)
    restore_mode(runner)
    assert load_saved_mode() is None


def test_restore_without_saved_state_is_a_noop():
    runner = FakeRunner()
    assert restore_mode(runner) is False
    assert not runner.calls


def test_failed_switch_does_not_leave_stale_state():
    """Otherwise a later restore would set a mode that was never applied."""
    runner = FakeRunner(fail_on="keyword")
    assert apply_stream_mode(runner) is None
    assert load_saved_mode() is None


def test_failed_restore_keeps_state_for_a_retry():
    save_mode(MonitorMode("eDP-2", 2560, 1600, 240.0, 0, 0, 1.6))
    assert restore_mode(FakeRunner(fail_on="keyword")) is False
    assert load_saved_mode() is not None


def test_crash_recovery_restores_on_a_later_run():
    """Simulates the daemon dying mid-cast: state survives, restore works."""
    apply_stream_mode(FakeRunner())
    later = FakeRunner()
    assert restore_mode(later) is True
    assert "2560x1600" in [c for c in later.calls if "keyword" in c][-1][-1]


def test_unparseable_monitor_output_is_handled():
    runner = FakeRunner("not json")
    assert focused_monitor(runner) is None
    assert apply_stream_mode(runner) is None


def test_corrupt_saved_state_is_discarded():
    from omarchy_cast.core.display import saved_mode_path
    saved_mode_path().parent.mkdir(parents=True, exist_ok=True)
    saved_mode_path().write_text("{garbage")
    assert load_saved_mode() is None
    assert not saved_mode_path().exists()


def test_does_nothing_without_hyprctl(monkeypatch):
    monkeypatch.setattr(display, "available", lambda: False)
    runner = FakeRunner()
    assert apply_stream_mode(runner) is None
    assert not runner.calls


def test_clear_saved_mode_is_safe_when_absent():
    clear_saved_mode()
    clear_saved_mode()
