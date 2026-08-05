import logging

from omarchy_cast.capture.encoder import gst_element_for
from omarchy_cast.core.config import Config

log = logging.getLogger(__name__)

# Verified against gst-inspect-1.0 and a live 2560x1600 capture.
#
# An explicit bitrate is REQUIRED: with rate-control=cbr and bitrate unset,
# vah264enc auto-calculated ~21 Mbps. A bounded GOP is REQUIRED so a receiver
# joining an in-progress stream gets a keyframe within about a second.
ENCODER_ARG_TEMPLATES = {
    "vaapi": "rate-control=cbr bitrate={bitrate} target-usage=6 key-int-max={gop}",
    "nvenc": "rc-mode=cbr bitrate={bitrate} gop-size={gop}",
    "x264": "tune=zerolatency speed-preset=veryfast bitrate={bitrate} key-int-max={gop}",
}


def encoder_args(encoder: str, config: Config) -> str:
    return ENCODER_ARG_TEMPLATES[encoder].format(
        bitrate=config.cast_bitrate, gop=config.fps
    )


def build_pipeline_description(node_id: int, fd: int, encoder: str, config: Config) -> str:
    element = gst_element_for(encoder)
    return (
        f"pipewiresrc path={node_id} fd={fd} do-timestamp=true ! "
        f"videorate ! video/x-raw,framerate={config.fps}/1 ! "
        f"videoconvert ! "
        f"{element} {encoder_args(encoder, config)} ! "
        f"h264parse config-interval=1 ! "
        f"matroskamux streamable=true ! "
        f"appsink emit-signals=true sync=false max-buffers=4 drop=true name=sink"
    )


class CapturePipeline:
    """Wraps a GStreamer pipeline whose appsink hands buffers to a callback.

    The callback runs on a GStreamer streaming thread, not the asyncio loop, so
    callers must marshal back themselves.
    """

    def __init__(self, description: str) -> None:
        self.description = description
        self._pipeline = None
        self._callback = None

    def set_sink_callback(self, callback) -> None:
        self._callback = callback

    def _on_sample(self, sink):
        from gi.repository import Gst

        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                if self._callback is not None:
                    self._callback(bytes(info.data))
            finally:
                buffer.unmap(info)
        return Gst.FlowReturn.OK

    def start(self) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self._pipeline = Gst.parse_launch(self.description)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        log.info("pipeline started: %s", self.description)

    def stop(self) -> None:
        if self._pipeline is None:
            return
        from gi.repository import Gst

        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
