"""Drives the real Textual app headlessly against a fake daemon."""

import asyncio

import pytest

from omarchy_cast.tui import app as tui_app

textual = pytest.importorskip("textual")


@pytest.fixture
def fake_daemon(monkeypatch):
    calls = []
    state = {
        "devices": [
            {"id": "airplay:1", "name": "Living Room", "protocol": "airplay",
             "model": "AppleTV14,1", "address": "192.168.1.5"},
            {"id": "cast:2", "name": "Bedroom", "protocol": "cast",
             "model": "Chromecast", "address": "192.168.1.6"},
        ],
        "sessions": [],
        "start_delay": 0.0,
    }

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
        if cmd == "start":
            # A real start takes ~6s. Instant fakes make concurrent presses
            # sequential and hide exactly the bug this suite must catch.
            await asyncio.sleep(state["start_delay"])
            calls.append(("start-completed", {}))
        if cmd == "list":
            return {"ok": True, "data": {"devices": state["devices"]}}
        if cmd == "status":
            return {"ok": True, "data": {"sessions": state["sessions"]}}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(tui_app, "request", _request)
    return calls, state


async def test_app_starts_and_lists_devices(fake_daemon):
    calls, _ = fake_daemon
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#devices")
        assert table.row_count == 2
        assert {c[0] for c in calls} >= {"list", "status"}


async def test_summary_shows_not_casting_when_idle(fake_daemon):
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Not casting" in str(app.query_one("#summary").content)


async def test_summary_reflects_an_active_session(fake_daemon):
    _, state = fake_daemon
    state["sessions"] = [{
        "id": "airplay:1", "name": "Living Room", "protocol": "airplay",
        "state": "streaming", "error": None,
    }]
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Living Room" in str(app.query_one("#summary").content)


async def test_pressing_enter_starts_the_selected_device(fake_daemon):
    calls, _ = fake_daemon
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#devices").focus()
        await pilot.press("enter")
        await pilot.pause()
        starts = [c for c in calls if c[0] == "start"]
        assert starts and starts[0][1]["device_id"] == "airplay:1"


async def test_pressing_s_stops(fake_daemon):
    calls, _ = fake_daemon
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert any(c[0] == "stop" for c in calls)


async def test_daemon_unavailable_is_shown_not_crashed(monkeypatch):
    async def _boom(cmd, path=None, **kwargs):
        raise tui_app.DaemonUnavailable("no daemon socket")

    monkeypatch.setattr(tui_app, "request", _boom)
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "unavailable" in str(app.query_one("#summary").content)


async def test_repeated_enter_starts_only_one_cast(fake_daemon):
    """Five Enter presses once opened five portal sessions and switched the
    display five times -- the screen appeared to loop. Start is now guarded.
    """
    calls, state = fake_daemon
    state["start_delay"] = 0.5
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#devices").focus()
        for _ in range(5):
            await pilot.press("enter")
        await pilot.pause()
        assert len([c for c in calls if c[0] == "start"]) == 1


async def test_start_is_refused_while_already_streaming(fake_daemon):
    calls, state = fake_daemon
    state["sessions"] = [{
        "id": "airplay:1", "name": "Living Room", "protocol": "airplay",
        "state": "streaming", "error": None,
    }]
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#devices").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not [c for c in calls if c[0] == "start"]
        assert "already" in str(app.query_one("#summary").content)


async def test_connecting_message_sets_expectations(fake_daemon):
    calls, state = fake_daemon
    state["start_delay"] = 0.5
    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#devices").focus()
        await pilot.press("enter")
        assert "few seconds" in str(app.query_one("#summary").content)


async def test_periodic_refresh_does_not_cancel_an_in_flight_start(fake_daemon):
    """Textual's exclusive=True cancels workers in the SAME group, and the
    default group is shared. With refresh and start in one group, the 2s
    refresh killed a ~6s start mid-flight: the cast aborted and the display
    bounced back, which looked like the screen looping.
    """
    calls, state = fake_daemon
    state["start_delay"] = 0.6

    app = tui_app.CastApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#devices").focus()
        await pilot.press("enter")

        # Refresh repeatedly while the start is still running.
        for _ in range(4):
            await asyncio.sleep(0.1)
            app.action_refresh()
        await asyncio.sleep(0.8)

        assert ("start-completed", {}) in calls, "refresh cancelled the start"


async def test_refresh_and_control_are_in_different_worker_groups():
    """Guards the fix directly: same group means they cancel each other."""
    import inspect
    src = inspect.getsource(tui_app)
    assert 'group="refresh"' in src
    assert 'group="control"' in src
