import time
import pytest

from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core import daemon as daemon_mod
from omarchy_cast.core.daemon import Daemon
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


class FakeDiscovery:
    def __init__(self, devices):
        self._devices = list(devices)
        self.started = False
        self.stopped = False

    def devices(self):
        return self._devices

    def add(self, device):
        self._devices.append(device)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_device(protocol="cast", ident="1"):
    return Device(
        id=Device.make_id(protocol, ident),
        name=f"{protocol}-{ident}",
        address="192.168.1.5",
        port=8009,
        protocol=protocol,
    )


def make_daemon(devices=None, **stub_kwargs):
    devices = devices if devices is not None else [make_device()]
    daemon = Daemon(FakeDiscovery(devices), {})
    daemon.backends["cast"] = StubBackend(daemon.on_state, **stub_kwargs)
    return daemon


async def test_list_returns_devices():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "list"})
    assert resp["ok"] is True
    assert resp["data"]["devices"][0]["id"] == "cast:1"


async def test_start_creates_streaming_session():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is True
    assert daemon.sessions["cast:1"].state is SessionState.STREAMING


async def test_start_unknown_device_errors():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:999"})
    assert resp["ok"] is False
    assert "not found" in resp["error"]


async def test_backend_failure_surfaces_message():
    daemon = make_daemon(fail_with="firewall blocked")
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is False
    assert "firewall blocked" in resp["error"]


async def test_status_reports_active_session():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    resp = await daemon.handle({"cmd": "status"})
    sessions = resp["data"]["sessions"]
    assert sessions[0]["state"] == "streaming"
    assert sessions[0]["name"] == "cast-1"


async def test_status_empty_when_idle():
    resp = await make_daemon().handle({"cmd": "status"})
    assert resp["data"]["sessions"] == []


async def test_stop_without_device_stops_all():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    resp = await daemon.handle({"cmd": "stop"})
    assert resp["ok"] is True
    assert daemon.sessions == {}


async def test_stop_unknown_device_errors():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "stop", "device_id": "cast:999"})
    assert resp["ok"] is False
    assert "no active session" in resp["error"]


async def test_stop_all_stops_every_session_even_when_one_reports_a_problem():
    """A stop that could not finish -- e.g. AirPlay failing to remove the
    virtual output -- must be reported, but must not abandon the sessions
    queued behind it."""
    from omarchy_cast.backends.base import BackendError

    stopped = []

    class ComplainingStop(StubBackend):
        async def stop(self, device):
            stopped.append(device.id)
            await super().stop(device)
            raise BackendError(f"could not remove the virtual output for {device.id}")

    daemon = make_daemon(devices=[make_device("cast", "1"), make_device("cast", "2")])
    daemon.backends["cast"] = ComplainingStop(daemon.on_state)
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    await daemon.handle({"cmd": "start", "device_id": "cast:2"})

    resp = await daemon.handle({"cmd": "stop"})
    assert resp["ok"] is False
    assert "could not remove" in resp["error"]
    assert sorted(stopped) == ["cast:1", "cast:2"]


async def test_pin_flow_reaches_streaming():
    daemon = make_daemon(needs_pin=True)
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions["cast:1"].state is SessionState.AWAITING_PIN
    resp = await daemon.handle({"cmd": "pin", "device_id": "cast:1", "pin": "1234"})
    assert resp["ok"] is True
    assert daemon.sessions["cast:1"].state is SessionState.STREAMING


async def test_pin_without_pending_session_errors():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "pin", "device_id": "cast:1", "pin": "1234"})
    assert resp["ok"] is False
    assert "no pending session" in resp["error"]


async def test_unknown_command_errors():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "frobnicate"})
    assert resp["ok"] is False
    assert "unknown command" in resp["error"]


async def test_no_backend_for_protocol_errors():
    daemon = make_daemon(devices=[make_device("airplay", "9")])
    resp = await daemon.handle({"cmd": "start", "device_id": "airplay:9"})
    assert resp["ok"] is False
    assert "no backend" in resp["error"]


async def test_failed_session_is_not_retained():
    """A failed session must not linger and block a retry."""
    daemon = make_daemon(fail_with="boom")
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions == {}


async def test_add_device_registers_manual_address():
    """mDNS is unusable on some networks; a raw address must still work."""
    daemon = make_daemon(devices=[])
    resp = await daemon.handle(
        {"cmd": "add", "address": "192.168.1.231", "protocol": "cast", "name": "Manual"}
    )
    assert resp["ok"] is True
    assert resp["data"]["device"]["address"] == "192.168.1.231"
    started = await daemon.handle({"cmd": "start", "device_id": resp["data"]["device"]["id"]})
    assert started["ok"] is True


async def test_add_device_rejects_bad_protocol():
    daemon = make_daemon(devices=[])
    resp = await daemon.handle({"cmd": "add", "address": "10.0.0.1", "protocol": "bogus"})
    assert resp["ok"] is False


async def test_add_device_requires_address():
    daemon = make_daemon(devices=[])
    resp = await daemon.handle({"cmd": "add", "protocol": "cast"})
    assert resp["ok"] is False
    assert "address" in resp["error"]


async def test_starting_a_cast_session_warns_it_is_untested():
    """Cast has never run against real hardware; say so at the point of use."""
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is True
    assert "UNTESTED" in resp["data"]["warning"]


async def test_airplay_start_carries_no_warning():
    daemon = make_daemon(devices=[make_device("airplay", "9")])
    daemon.backends["airplay"] = StubBackend(daemon.on_state)
    resp = await daemon.handle({"cmd": "start", "device_id": "airplay:9"})
    assert "warning" not in resp["data"]


async def test_backend_failures_are_logged_not_just_returned(caplog):
    """A client that disconnects mid-request must not take the reason with it."""
    import logging

    daemon = make_daemon(fail_with="something went wrong")
    with caplog.at_level(logging.ERROR):
        resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is False
    assert "something went wrong" in caplog.text


async def test_serve_installs_signal_handlers(tmp_path):
    """SIGTERM must unwind serve() rather than killing the process outright,
    otherwise doubletake and its capture pipelines are orphaned on logout.
    """
    import asyncio
    import signal

    daemon = make_daemon()
    sock = tmp_path / "sig.sock"
    task = asyncio.create_task(daemon.serve(sock))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if sock.exists():
            break

    loop = asyncio.get_running_loop()
    # A handler registered for SIGTERM means the daemon will clean up.
    daemon._on_signal(signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)
    assert not sock.exists(), "socket should be removed on shutdown"


# -- list waits for a cold mDNS browser -------------------------------------


class SlowDiscovery:
    """Finds a device only after `appear_after` calls to devices(), modelling
    mDNS taking a moment to hear back rather than failing."""

    def __init__(self, device, appear_after):
        self._device = device
        self._appear_after = appear_after
        self.calls = 0

    def devices(self):
        self.calls += 1
        return [self._device] if self.calls > self._appear_after else []

    def add(self, device):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def _cast_device():
    return Device(
        id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast"
    )


async def test_list_waits_for_a_cold_discovery_to_find_something():
    """The daemon exits after 30s idle, so nearly every `list` -- and so nearly
    every press of the cast keybind -- hits a browser that has not heard back
    yet. Answering instantly reported an empty network with a receiver sitting
    right there."""
    discovery = SlowDiscovery(_cast_device(), appear_after=3)
    daemon = Daemon(discovery, {}, notifier=lambda m: None)
    daemon._discovery_started_at = time.monotonic()

    resp = await daemon.handle({"cmd": "list"})

    assert [d["id"] for d in resp["data"]["devices"]] == ["cast:1"]


async def test_list_does_not_wait_once_something_is_known():
    """A warm daemon must stay instant -- the wait is for a cold start only."""
    discovery = SlowDiscovery(_cast_device(), appear_after=0)
    daemon = Daemon(discovery, {}, notifier=lambda m: None)
    daemon._discovery_started_at = time.monotonic()

    started = time.monotonic()
    resp = await daemon.handle({"cmd": "list"})

    assert len(resp["data"]["devices"]) == 1
    assert time.monotonic() - started < 0.1


async def test_list_gives_up_once_the_grace_period_has_passed():
    """A genuinely empty network must not pay the wait on every later call."""
    discovery = SlowDiscovery(_cast_device(), appear_after=10_000)
    daemon = Daemon(discovery, {}, notifier=lambda m: None)
    daemon._discovery_started_at = time.monotonic() - (daemon_mod.DISCOVERY_GRACE + 1)

    started = time.monotonic()
    resp = await daemon.handle({"cmd": "list"})

    assert resp["data"]["devices"] == []
    assert time.monotonic() - started < 0.1


async def test_list_stops_waiting_at_the_grace_ceiling():
    """It must bound the wait, not block the keybind indefinitely."""
    discovery = SlowDiscovery(_cast_device(), appear_after=10_000)
    daemon = Daemon(discovery, {}, notifier=lambda m: None)
    monkeypatched_grace = 0.3
    daemon._discovery_started_at = (
        time.monotonic() - daemon_mod.DISCOVERY_GRACE + monkeypatched_grace
    )

    started = time.monotonic()
    resp = await daemon.handle({"cmd": "list"})
    waited = time.monotonic() - started

    assert resp["data"]["devices"] == []
    assert monkeypatched_grace - 0.15 < waited < monkeypatched_grace + 0.3
