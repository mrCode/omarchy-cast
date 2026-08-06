import json

import pytest

from omarchy_cast.core import virtual_display
from omarchy_cast.core.virtual_display import (
    VIRTUAL_NAME,
    cleanup_strays,
    create,
    remove,
)


@pytest.fixture(autouse=True)
def hyprctl_present(monkeypatch):
    monkeypatch.setattr(virtual_display, "available", lambda: True)


def monitors(*names):
    return json.dumps([
        {"name": n, "width": 1920, "height": 1080, "refreshRate": 60.0,
         "x": 0, "y": 0, "scale": 1.0}
        for n in names
    ])


class FakeRunner:
    """Simulates hyprctl: `output create` adds a monitor, `remove` deletes it."""

    def __init__(self, existing=("eDP-2",), created_as=None, fail_on=None):
        self.names = list(existing)
        self._created_as = created_as
        self._fail_on = fail_on
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        joined = " ".join(argv)
        if self._fail_on and self._fail_on in joined:
            return 1, ""
        if "monitors" in argv:
            return 0, monitors(*self.names)
        if "create" in argv:
            # Hyprland may ignore the requested name and use HEADLESS-N.
            self.names.append(self._created_as or VIRTUAL_NAME)
            return 0, "ok"
        if "remove" in argv:
            target = argv[-1]
            if target in self.names:
                self.names.remove(target)
            return 0, "ok"
        return 0, "ok"


def test_create_returns_the_new_output_name():
    runner = FakeRunner()
    assert create(runner) == VIRTUAL_NAME
    assert VIRTUAL_NAME in runner.names


def test_create_returns_the_observed_name_not_the_requested_one():
    """Design testing left a stray HEADLESS-2 by trusting the requested name."""
    runner = FakeRunner(created_as="HEADLESS-7")
    assert create(runner) == "HEADLESS-7"


def test_create_configures_1080p_at_scale_1():
    """Default scale is 2.0, giving a useless logical 960x540."""
    runner = FakeRunner()
    create(runner)
    keyword = [c for c in runner.calls if "keyword" in c][-1]
    assert "1920x1080@60" in keyword[-1]
    assert keyword[-1].endswith(",auto,1")
    assert keyword[-1].startswith(f"{VIRTUAL_NAME},")


def test_create_returns_none_when_creation_fails():
    runner = FakeRunner(fail_on="output create")
    assert create(runner) is None


def test_create_returns_none_when_no_new_monitor_appears():
    class Silent(FakeRunner):
        def __call__(self, argv):
            self.calls.append(argv)
            if "monitors" in argv:
                return 0, monitors(*self.names)
            return 0, "ok"

    assert create(Silent()) is None


def test_remove_deletes_the_output():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME))
    assert remove(VIRTUAL_NAME, runner) is True
    assert VIRTUAL_NAME not in runner.names


def test_remove_reports_failure():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME), fail_on="output remove")
    assert remove(VIRTUAL_NAME, runner) is False


def test_cleanup_removes_strays_including_headless():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME, "HEADLESS-2"))
    assert cleanup_strays(runner) == 2
    assert runner.names == ["eDP-2"]


def test_cleanup_leaves_real_monitors_alone():
    runner = FakeRunner(existing=("eDP-2", "HDMI-A-1"))
    assert cleanup_strays(runner) == 0
    assert runner.names == ["eDP-2", "HDMI-A-1"]


def test_nothing_happens_without_hyprctl(monkeypatch):
    monkeypatch.setattr(virtual_display, "available", lambda: False)
    runner = FakeRunner()
    assert create(runner) is None
    assert not runner.calls


def test_unparseable_monitor_output_is_handled():
    class Garbage(FakeRunner):
        def __call__(self, argv):
            self.calls.append(argv)
            return 0, "not json"

    assert create(Garbage()) is None
