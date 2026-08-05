import pytest

from omarchy_cast.backends.base import BackendError
from omarchy_cast.backends.cast import CAST_APP_ID, CastBackend, host_tuple
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


def make_device():
    return Device(
        id="cast:1", name="Bedroom", address="192.168.1.50", port=8009, protocol="cast"
    )


class FakeCapture:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.url = None

    async def start(self, device):
        self.started = True
        self.url = "http://192.168.1.10:9999/stream.mkv"
        return self.url

    async def stop(self):
        self.stopped = True


class FailingCapture(FakeCapture):
    async def start(self, device):
        raise BackendError("no H.264 encoder found")


class FakeMediaController:
    def __init__(self, fail=False):
        self.played = None
        self._fail = fail

    def play_media(self, url, content_type, **kwargs):
        if self._fail:
            raise RuntimeError("receiver refused")
        self.played = (url, content_type, kwargs)

    def stop(self):
        self.played = None


class FakeChromecast:
    def __init__(self, fail_connect=False, fail_play=False):
        self.app_id = None
        self.media_controller = FakeMediaController(fail=fail_play)
        self.disconnected = False
        self._fail_connect = fail_connect

    def wait(self, timeout=None):
        if self._fail_connect:
            raise OSError("unreachable")

    def start_app(self, app_id):
        self.app_id = app_id

    def quit_app(self):
        self.app_id = None

    def disconnect(self):
        self.disconnected = True


def make_backend(capture=None, cast=None):
    states = []
    capture = capture if capture is not None else FakeCapture()
    cast = cast or FakeChromecast()
    backend = CastBackend(
        lambda d, s, e: states.append((s, e)),
        Config(),
        capture_factory=lambda cfg: capture,
        chromecast_factory=lambda device: cast,
    )
    return backend, states, capture, cast


def test_host_tuple_shape():
    """pychromecast wants (host, port, uuid, model, friendly_name)."""
    t = host_tuple(make_device())
    assert t[0] == "192.168.1.50"
    assert t[1] == 8009
    assert len(t) == 5


async def test_start_launches_default_media_receiver():
    backend, states, capture, cast = make_backend()
    await backend.start(make_device())
    assert cast.app_id == CAST_APP_ID
    assert capture.started is True
    assert states[0][0] is SessionState.CONNECTING
    assert states[-1][0] is SessionState.STREAMING


async def test_start_loads_stream_url_as_live_matroska():
    backend, _, capture, cast = make_backend()
    await backend.start(make_device())
    url, content_type, kwargs = cast.media_controller.played
    assert url == capture.url
    assert content_type == "video/x-matroska"
    assert kwargs["stream_type"] == "LIVE"


async def test_unreachable_device_is_actionable():
    backend, states, capture, _ = make_backend(cast=FakeChromecast(fail_connect=True))
    with pytest.raises(BackendError, match="could not connect"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED
    assert capture.stopped is True


async def test_receiver_refusing_media_stops_capture():
    backend, states, capture, _ = make_backend(cast=FakeChromecast(fail_play=True))
    with pytest.raises(BackendError):
        await backend.start(make_device())
    assert capture.stopped is True
    assert states[-1][0] is SessionState.FAILED


async def test_refusal_message_mentions_reachability():
    """The usual cause is the receiver being unable to reach our HTTP server."""
    backend, _, _, _ = make_backend(cast=FakeChromecast(fail_play=True))
    with pytest.raises(BackendError, match="reach"):
        await backend.start(make_device())


async def test_capture_failure_surfaces_directly():
    backend, states, _, cast = make_backend(capture=FailingCapture())
    with pytest.raises(BackendError, match="no H.264 encoder"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED
    assert cast.app_id is None


async def test_stop_quits_app_and_stops_capture():
    backend, states, capture, cast = make_backend()
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert cast.app_id is None
    assert capture.stopped is True
    assert cast.disconnected is True
    assert states[-1][0] is SessionState.IDLE


async def test_shutdown_tears_everything_down():
    backend, _, capture, cast = make_backend()
    await backend.start(make_device())
    await backend.shutdown()
    assert capture.stopped is True
    assert cast.disconnected is True


async def test_cast_does_not_support_pin():
    backend, _, _, _ = make_backend()
    with pytest.raises(BackendError, match="PIN"):
        await backend.submit_pin(make_device(), "1234")


async def test_stop_without_a_session_is_safe():
    backend, states, _, _ = make_backend()
    await backend.stop(make_device())
    assert states[-1][0] is SessionState.IDLE
