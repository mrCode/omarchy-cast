from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core.daemon import Daemon
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


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


def make_device():
    return Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")


def make_daemon(**stub_kwargs):
    sent = []
    daemon = Daemon(FakeDiscovery([make_device()]), {}, notifier=sent.append)
    daemon.backends["cast"] = StubBackend(daemon.on_state, **stub_kwargs)
    return daemon, sent


async def test_failure_notifies_the_user():
    daemon, sent = make_daemon(fail_with="mirroring stopped unexpectedly")
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert len(sent) == 1
    assert "mirroring stopped unexpectedly" in sent[0]


async def test_normal_stop_does_not_notify():
    daemon, sent = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    await daemon.handle({"cmd": "stop"})
    assert sent == []


async def test_notifier_failure_does_not_break_the_daemon():
    """notify-send may not be installed; that must not take the daemon down."""

    def boom(message):
        raise OSError("notify-send missing")

    daemon = Daemon(FakeDiscovery([make_device()]), {}, notifier=boom)
    daemon.backends["cast"] = StubBackend(daemon.on_state, fail_with="boom")
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is False


async def test_session_still_cleared_after_notifying():
    daemon, _ = make_daemon(fail_with="boom")
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions == {}


async def test_late_emit_after_failure_is_ignored_not_raised():
    """Backends emit from background tasks; a late emit must not crash them."""
    daemon, sent = make_daemon(fail_with="boom")
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    # The session is already gone; this transition is illegal from IDLE.
    daemon.on_state(make_device(), SessionState.STOPPING, None)
    assert len(sent) == 1
