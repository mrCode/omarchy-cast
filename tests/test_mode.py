import pytest

from omarchy_cast.backends.creds import EXTEND, MIRROR
from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core.daemon import Daemon
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import Session


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
