import asyncio

import pytest

from omarchy_cast.backends.base import BackendError
from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core.daemon import Daemon
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import EXTEND, MIRROR, Session


class FakeDiscovery:
    def __init__(self, devices):
        self._devices = list(devices)

    def devices(self):
        return self._devices

    def add(self, device):
        self._devices.append(device)

    def start(self):
        pass

    def stop(self):
        pass


def make_device(protocol="cast", ident="1"):
    return Device(
        id=Device.make_id(protocol, ident), name=f"{protocol}-{ident}",
        address="192.168.1.5", port=8009, protocol=protocol,
    )


def make_daemon():
    daemon = Daemon(FakeDiscovery([make_device()]), {}, notifier=lambda m: None)
    daemon.backends["cast"] = StubBackend(daemon.on_state)
    return daemon


def test_session_defaults_to_mirror():
    assert Session(make_device()).mode == MIRROR


def test_session_records_its_mode():
    assert Session(make_device(), mode=EXTEND).mode == EXTEND


async def test_start_defaults_to_mirror():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions["cast:1"].mode == MIRROR


async def test_start_accepts_extend():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1", "mode": "extend"})
    assert daemon.sessions["cast:1"].mode == EXTEND


async def test_start_rejects_an_unknown_mode():
    daemon = make_daemon()
    resp = await daemon.handle(
        {"cmd": "start", "device_id": "cast:1", "mode": "sideways"}
    )
    assert resp["ok"] is False
    assert "sideways" in resp["error"]


async def test_status_reports_the_mode():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1", "mode": "extend"})
    resp = await daemon.handle({"cmd": "status"})
    assert resp["data"]["sessions"][0]["mode"] == EXTEND


async def test_backend_receives_the_mode():
    seen = {}

    class Recording(StubBackend):
        async def start(self, device, mode=MIRROR):
            seen["mode"] = mode
            await super().start(device)

    daemon = make_daemon()
    daemon.backends["cast"] = Recording(daemon.on_state)
    await daemon.handle({"cmd": "start", "device_id": "cast:1", "mode": "extend"})
    assert seen["mode"] == EXTEND


async def test_concurrent_starts_do_not_cross_wires_on_mode():
    """Regression test: mode must not travel through shared daemon state.

    A backend that suspends before its first state emit (as AirPlayBackend
    does -- it awaits a teardown of up to 2s before emitting CONNECTING) used
    to leave a window where a second client's concurrent start could
    overwrite a daemon-wide "pending mode" before the first device's session
    was ever created from it, mislabelling the first device.
    """
    device_a = make_device(ident="1")
    device_b = make_device(ident="2")
    release = asyncio.Event()

    class SlowStart(StubBackend):
        async def start(self, device, mode=MIRROR):
            if device.id == device_a.id:
                await release.wait()
            await super().start(device, mode)

    daemon = Daemon(
        FakeDiscovery([device_a, device_b]), {}, notifier=lambda m: None
    )
    daemon.backends["cast"] = SlowStart(daemon.on_state)

    # device_a asks for extend and immediately blocks before its first emit.
    task_a = asyncio.create_task(
        daemon.handle({"cmd": "start", "device_id": device_a.id, "mode": "extend"})
    )
    await asyncio.sleep(0)  # let task_a run up to release.wait()

    # device_b's start (mirror, the default) runs to completion while task_a
    # is still suspended.
    resp_b = await daemon.handle(
        {"cmd": "start", "device_id": device_b.id, "mode": "mirror"}
    )
    assert resp_b["ok"] is True

    release.set()
    await task_a

    assert daemon.sessions[device_a.id].mode == EXTEND
    assert daemon.sessions[device_b.id].mode == MIRROR


async def test_a_start_that_raises_before_any_emit_leaves_no_stale_session():
    """The session created up front for the new mode must not linger if the
    backend fails before ever transitioning it out of IDLE.
    """

    class ExplodesImmediately(StubBackend):
        async def start(self, device, mode=MIRROR):
            raise BackendError("boom before any emit")

    daemon = make_daemon()
    daemon.backends["cast"] = ExplodesImmediately(daemon.on_state)
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is False
    assert daemon.sessions == {}


async def test_stop_during_the_pre_emit_window_reports_no_active_session():
    """Regression test: a session registered by _cmd_start but not yet
    emitted from (e.g. during AirPlay's up-to-2s pre-start teardown) is not
    actually running. Before the fix, stop found it, called backend.stop()
    (a no-op the backend doesn't recognise since nothing is registered there
    yet, its STOPPING/IDLE emits silently swallowed as illegal transitions
    from IDLE), and still reported {"stopped": 1} -- success with nothing
    stopped, while the device carried on connecting.
    """
    device = make_device()
    release = asyncio.Event()

    class SlowStart(StubBackend):
        async def start(self, device, mode=MIRROR):
            await release.wait()
            await super().start(device, mode)

    daemon = make_daemon()
    daemon.backends["cast"] = SlowStart(daemon.on_state)

    task = asyncio.create_task(daemon.handle({"cmd": "start", "device_id": device.id}))
    await asyncio.sleep(0)  # let the start register its session and suspend

    resp = await daemon.handle({"cmd": "stop", "device_id": device.id})
    assert resp["ok"] is False
    assert "no active session" in resp["error"]

    # The stop must not have disturbed the in-flight start.
    release.set()
    start_resp = await task
    assert start_resp["ok"] is True
    assert daemon.sessions[device.id].state == "streaming"


async def test_stop_all_skips_sessions_in_the_pre_emit_window():
    device = make_device()
    release = asyncio.Event()

    class SlowStart(StubBackend):
        async def start(self, device, mode=MIRROR):
            await release.wait()
            await super().start(device, mode)

    daemon = make_daemon()
    daemon.backends["cast"] = SlowStart(daemon.on_state)

    task = asyncio.create_task(daemon.handle({"cmd": "start", "device_id": device.id}))
    await asyncio.sleep(0)

    resp = await daemon.handle({"cmd": "stop"})
    assert resp["ok"] is True
    assert resp["data"]["stopped"] == 0

    release.set()
    await task


async def test_status_omits_sessions_in_the_pre_emit_window():
    """_cmd_stop and _cmd_pin were taught that an IDLE session is not real yet;
    _cmd_status was not, and waybar's render() has no idle branch, so such a
    session fell past `failed` and `connecting` into the streaming return. For
    up to 2s after restarting an AirPlay cast on a device that already had a
    session, waybar showed a green streaming indicator offering
    "Stop casting (dev1)" -- which _cmd_stop then refused with an error.
    """
    from omarchy_cast.cli.waybar import render

    device = make_device()
    release = asyncio.Event()

    class SlowStart(StubBackend):
        async def start(self, device, mode=MIRROR):
            await release.wait()
            await super().start(device, mode)

    daemon = make_daemon()
    daemon.backends["cast"] = SlowStart(daemon.on_state)

    task = asyncio.create_task(daemon.handle({"cmd": "start", "device_id": device.id}))
    await asyncio.sleep(0)  # let the start register its session and suspend

    resp = await daemon.handle({"cmd": "status"})
    assert resp["data"]["sessions"] == []
    assert render(resp["data"]["sessions"])["class"] != "streaming"

    release.set()
    await task

    # Once the backend has actually emitted, it is reported again.
    resp = await daemon.handle({"cmd": "status"})
    assert [s["state"] for s in resp["data"]["sessions"]] == ["streaming"]
    assert render(resp["data"]["sessions"])["class"] == "streaming"


async def test_pin_during_the_pre_emit_window_reports_no_pending_session():
    """Same shape as the stop regression: submitting a PIN to a session whose
    backend has not started yet must fail rather than silently do nothing.
    """
    device = make_device()
    release = asyncio.Event()

    class SlowStart(StubBackend):
        async def start(self, device, mode=MIRROR):
            await release.wait()
            await super().start(device, mode)

    daemon = make_daemon()
    daemon.backends["cast"] = SlowStart(daemon.on_state)

    task = asyncio.create_task(daemon.handle({"cmd": "start", "device_id": device.id}))
    await asyncio.sleep(0)

    resp = await daemon.handle({"cmd": "pin", "device_id": device.id, "pin": "1234"})
    assert resp["ok"] is False
    assert "no pending session" in resp["error"]

    release.set()
    await task


def test_cli_passes_the_mode(monkeypatch):
    from omarchy_cast.cli import main as cli_main

    calls = []

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
        return {"ok": True, "data": {"state": "streaming"}}

    monkeypatch.setattr(cli_main, "request", _request)
    assert cli_main.main(["start", "cast:1", "--mode", "extend"]) == 0
    assert calls[0][1]["mode"] == "extend"


def test_cli_defaults_to_mirror(monkeypatch):
    from omarchy_cast.cli import main as cli_main

    calls = []

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
        return {"ok": True, "data": {"state": "streaming"}}

    monkeypatch.setattr(cli_main, "request", _request)
    cli_main.main(["start", "cast:1"])
    assert calls[0][1]["mode"] == "mirror"


def test_cli_rejects_a_bad_mode(monkeypatch):
    from omarchy_cast.cli import main as cli_main

    with pytest.raises(SystemExit):
        cli_main.main(["start", "cast:1", "--mode", "sideways"])
