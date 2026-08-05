"""Drives the real Textual app headlessly against a fake daemon."""

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
    }

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
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
