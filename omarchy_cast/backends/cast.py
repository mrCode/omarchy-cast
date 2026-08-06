import asyncio
import logging
import uuid as uuid_module

from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.capture.encoder import (
    NoEncoderAvailable,
    probe_available,
    select_encoder,
)
from omarchy_cast.capture.http import CONTENT_TYPE, StreamServer, local_address_for
from omarchy_cast.capture.pipeline import CapturePipeline, build_pipeline_description
from omarchy_cast.capture.portal import PortalError, open_screencast
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import EXTEND, MIRROR, SessionState

log = logging.getLogger(__name__)

CAST_APP_ID = "CC1AD845"  # Default Media Receiver
CONNECT_TIMEOUT = 10.0


def host_tuple(device: Device) -> tuple:
    """Build the host tuple pychromecast.get_chromecast_from_host expects:
    (host, port, uuid, model_name, friendly_name).
    """
    return (
        device.address,
        device.port,
        uuid_module.uuid5(uuid_module.NAMESPACE_DNS, device.address),
        device.model,
        device.name,
    )


class CaptureService:
    """Owns the portal session, the GStreamer pipeline, and the HTTP server."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._pipeline: CapturePipeline | None = None
        self._server: StreamServer | None = None

    async def start(self, device: Device) -> str:
        try:
            encoder = select_encoder(self._config, probe_available())
        except NoEncoderAvailable as exc:
            raise BackendError(str(exc)) from exc

        try:
            portal = await open_screencast()
        except PortalError as exc:
            raise BackendError(str(exc)) from exc

        host = local_address_for(device.address)
        self._server = StreamServer(host, self._config.cast_http_port)
        port = await self._server.start()

        description = build_pipeline_description(
            portal.node_id, portal.fd, encoder, self._config
        )
        self._pipeline = CapturePipeline(description)

        # The appsink callback runs on a GStreamer thread, not the event loop.
        loop = asyncio.get_running_loop()
        server = self._server
        self._pipeline.set_sink_callback(
            lambda chunk: loop.call_soon_threadsafe(server.push, chunk)
        )
        self._pipeline.start()
        return f"http://{host}:{port}{StreamServer.url_path}"

    async def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
        if self._server is not None:
            await self._server.stop()
            self._server = None


def _default_chromecast_factory(device: Device):
    import pychromecast

    return pychromecast.get_chromecast_from_host(host_tuple(device))


class CastBackend(Backend):
    """Casts to a Chromecast via the Default Media Receiver.

    Latency is 1-3 seconds because the receiver buffers a media stream. This is
    not the low-latency Chrome Mirroring path, which needs AES-CTR-128 that
    GStreamer's SRTP elements do not provide; see the design doc.
    """

    protocol = "cast"

    def __init__(
        self,
        on_state: StateCallback,
        config: Config,
        *,
        capture_factory=None,
        chromecast_factory=None,
    ) -> None:
        super().__init__(on_state)
        self._config = config
        self._capture_factory = capture_factory or CaptureService
        self._chromecast_factory = chromecast_factory or _default_chromecast_factory
        self._capture = None
        self._cast = None

    async def start(self, device: Device, mode: str = MIRROR) -> None:
        if mode == EXTEND:
            # Extend means the receiver becomes a second display. The Default
            # Media Receiver buffers a media stream, so a window dragged onto
            # it would respond a second or more late -- and this path captures
            # the real screen, with no virtual output anywhere in it.
            # Accepting the mode and ignoring it, as this did, made status,
            # waybar and the menu all report an extend that was really a
            # mirror, and pointed the user at an 'omarchy-cast' output that
            # did not exist.
            message = (
                "extend is AirPlay-only; Chromecast buffers 1-3s and cannot "
                "serve as a second display"
            )
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message)

        self._emit(device, SessionState.CONNECTING)
        self._capture = self._capture_factory(self._config)

        try:
            url = await self._capture.start(device)
        except BackendError as exc:
            await self._fail(device, str(exc))
            raise

        try:
            self._cast = self._chromecast_factory(device)
            await asyncio.to_thread(self._cast.wait, CONNECT_TIMEOUT)
            self._cast.start_app(CAST_APP_ID)
            self._cast.media_controller.play_media(
                url, CONTENT_TYPE, stream_type="LIVE", title="Omarchy"
            )
        except OSError as exc:
            message = f"could not connect to {device.name} at {device.address}: {exc}"
            await self._fail(device, message)
            raise BackendError(message) from exc
        except Exception as exc:
            message = (
                f"{device.name} refused the stream: {exc}. The receiver must be "
                f"able to reach {url} -- check that no firewall blocks that port."
            )
            await self._fail(device, message)
            raise BackendError(message) from exc

        self._emit(device, SessionState.STREAMING)

    async def _fail(self, device: Device, message: str) -> None:
        await self._teardown()
        self._emit(device, SessionState.FAILED, message)

    async def _teardown(self) -> None:
        if self._capture is not None:
            await self._capture.stop()
            self._capture = None
        if self._cast is not None:
            try:
                self._cast.quit_app()
                self._cast.disconnect()
            except Exception:
                log.debug("error while disconnecting cast device", exc_info=True)
            self._cast = None

    async def stop(self, device: Device) -> None:
        self._emit(device, SessionState.STOPPING)
        await self._teardown()
        self._emit(device, SessionState.IDLE)

    async def shutdown(self) -> None:
        await self._teardown()
