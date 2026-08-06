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


def test_create_returns_none_and_removes_output_when_geometry_fails():
    """Half-configured outputs must not be left behind; cleanup_strays exists to
    prevent desktop strays, not to handle create() failures."""
    runner = FakeRunner(fail_on="keyword")
    assert create(runner) is None
    # The output was created but geometry failed, so it should have been removed.
    assert VIRTUAL_NAME not in runner.names


class BlindAfterCreate(FakeRunner):
    """The post-create `hyprctl monitors` read fails; the first one succeeded.

    `mode="error"` is a non-zero exit, `mode="garbage"` unparseable JSON.
    """

    def __init__(self, mode="error", **kwargs):
        super().__init__(**kwargs)
        self._mode = mode
        self._created = False

    def __call__(self, argv):
        if "monitors" in argv and self._created:
            self.calls.append(argv)
            return (1, "") if self._mode == "error" else (0, "not json")
        result = super().__call__(argv)
        if "create" in argv:
            self._created = True
        return result


def test_create_removes_the_output_when_the_post_create_read_fails():
    """`return None` here left the output that was just created in place: a
    stray 1920x1080 monitor on the desktop, while start() reported that no
    virtual display could be created."""
    runner = BlindAfterCreate(mode="error")
    assert create(runner) is None
    assert runner.names == ["eDP-2"]


def test_create_removes_the_output_when_the_post_create_read_is_unparseable():
    runner = BlindAfterCreate(mode="garbage")
    assert create(runner) is None
    assert runner.names == ["eDP-2"]


def test_create_returns_none_when_no_new_monitor_appears():
    class Invisible(FakeRunner):
        """hyprctl reports the create succeeded but never lists the output."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._visible = list(self.names)

        def __call__(self, argv):
            if "monitors" in argv:
                self.calls.append(argv)
                return 0, monitors(*self._visible)
            return super().__call__(argv)

    runner = Invisible()
    assert create(runner) is None
    # Nothing was left behind, even though the diff could not name it.
    assert runner.names == ["eDP-2"]


def test_remove_deletes_the_output():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME))
    assert remove(VIRTUAL_NAME, runner) is True
    assert VIRTUAL_NAME not in runner.names


def test_remove_reports_failure():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME), fail_on="output remove")
    assert remove(VIRTUAL_NAME, runner) is False


def test_cleanup_removes_our_own_stray_output():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME))
    assert cleanup_strays(runner) == 1
    assert runner.names == ["eDP-2"]


def test_cleanup_leaves_headless_outputs_it_did_not_create():
    """wayvnc and Sunshine both create HEADLESS-N outputs. Sweeping every
    headless output destroyed a live one the user owned -- silently, on any
    omarchy-cast invocation, since this runs at daemon start and on every
    extend."""
    runner = FakeRunner(existing=("eDP-2", "HEADLESS-1", VIRTUAL_NAME))
    assert cleanup_strays(runner) == 1
    assert runner.names == ["eDP-2", "HEADLESS-1"]


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
