import pytest

from omarchy_cast.backends.base import Backend, BackendError
from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


def make_device():
    return Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")


def test_backend_is_abstract():
    with pytest.raises(TypeError):
        Backend(lambda *a: None)


async def test_stub_reports_connecting_then_streaming():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append((s, e)))
    await backend.start(make_device())
    assert seen == [
        (SessionState.CONNECTING, None),
        (SessionState.STREAMING, None),
    ]


async def test_stub_can_simulate_failure():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append((s, e)), fail_with="nope")
    with pytest.raises(BackendError, match="nope"):
        await backend.start(make_device())
    assert seen[-1] == (SessionState.FAILED, "nope")


async def test_stub_pin_flow():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append(s), needs_pin=True)
    device = make_device()
    await backend.start(device)
    assert seen[-1] is SessionState.AWAITING_PIN
    await backend.submit_pin(device, "1234")
    assert seen[-1] is SessionState.STREAMING


async def test_stub_stop_returns_to_idle():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append(s))
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert seen[-2:] == [SessionState.STOPPING, SessionState.IDLE]


async def test_submit_pin_unsupported_by_default():
    """Cast has no PIN pairing; the base class must say so clearly."""

    class NoPin(Backend):
        protocol = "cast"

        async def start(self, device):
            pass

        async def stop(self, device):
            pass

    with pytest.raises(BackendError, match="PIN"):
        await NoPin(lambda *a: None).submit_pin(make_device(), "1234")


async def test_shutdown_is_a_noop_by_default():
    class Minimal(Backend):
        protocol = "cast"

        async def start(self, device):
            pass

        async def stop(self, device):
            pass

    assert await Minimal(lambda *a: None).shutdown() is None
