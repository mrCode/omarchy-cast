import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENCODERS = ("auto", "vaapi", "nvenc", "x264")
DEFAULT_RANKING = ["vaapi", "x264", "nvenc"]


def default_config_path() -> Path:
    return Path.home() / ".config" / "omarchy-cast" / "config.toml"


@dataclass
class Config:
    fps: int = 30
    encoder: str = "auto"
    encoder_ranking: list[str] = field(default_factory=lambda: list(DEFAULT_RANKING))
    # Honoured: the AirPlay backend runs doubletake directly, where
    # -port-range works. It is ignored in doubletake's daemon mode, which is
    # why we do not use it.
    airplay_port_range: str = "60000-60010"
    airplay_bitrate: int = 0
    airplay_code: str = ""
    # doubletake's capture pipeline uses vapostproc to import the portal's
    # DMA-BUF. On Hyprland the buffer is padded (16 MiB for a 2560x1600
    # RGBA frame vs a 15.6 MiB descriptor) and GStreamer's VA allocator
    # refuses it, producing a silent black screen. Hiding the element makes
    # doubletake fall back to videoconvert, which works. Costs some CPU.
    airplay_hide_vapostproc: bool = True
    # doubletake negotiates 1920x1080 and its fallback capture path has no
    # scaler, so a higher-resolution display makes the receiver drop the
    # connection. Switch the display while casting and put it back after.
    airplay_auto_resolution: bool = True
    # Seconds to wait for doubletake to report "screen capture started".
    # 30 was too tight: measured on an AppleTV11,1, capture began 23s after
    # "mirror session ready", and extend adds a portal round-trip on top, so
    # extend timed out repeatedly on a machine where mirror just squeaked
    # through. Raising this costs nothing when a cast succeeds -- the wait ends
    # at the marker, not at the ceiling.
    airplay_ready_timeout: float = 60.0
    # doubletake's -target-latency-ms: how much end-to-end delay the sender
    # targets, which the receiver buffers to. Its default is 100. Lower means
    # a more responsive cursor and less typing lag, at the cost of tolerance
    # for network jitter -- too low and the receiver stutters.
    airplay_target_latency_ms: int = 100
    # Audio is streamed by default. Turning it off removes audio/video sync
    # from the pipeline, which is worth testing when mirroring a desktop for
    # work rather than playing media.
    airplay_audio: bool = True
    # Fixed, not ephemeral: the receiver connects INTO this port to fetch the
    # stream, so a random port in the ephemeral range cannot be allowed
    # through a firewall ahead of time. Set to 0 only if you have no
    # firewall and want the OS to pick.
    cast_http_port: int = 8010
    # 8000 kbps measured at ~7 Mbps actual on 2560x1600@30. Without an explicit
    # bitrate, rate-control=cbr auto-calculated ~21 Mbps.
    cast_bitrate: int = 8000


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    cfg = Config()
    if not path.exists():
        return cfg

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    capture = data.get("capture", {})
    if "fps" in capture:
        cfg.fps = int(capture["fps"])
    if "encoder" in capture:
        cfg.encoder = str(capture["encoder"])
    if "encoder_ranking" in capture:
        cfg.encoder_ranking = list(capture["encoder_ranking"])

    airplay = data.get("airplay", {})
    if "port_range" in airplay:
        cfg.airplay_port_range = str(airplay["port_range"])
    if "bitrate" in airplay:
        cfg.airplay_bitrate = int(airplay["bitrate"])
    if "code" in airplay:
        # Passed to doubletake as DOUBLETAKE_CODE once upstream #26 merges.
        cfg.airplay_code = str(airplay["code"])
    if "hide_vapostproc" in airplay:
        cfg.airplay_hide_vapostproc = bool(airplay["hide_vapostproc"])
    if "auto_resolution" in airplay:
        cfg.airplay_auto_resolution = bool(airplay["auto_resolution"])
    if "ready_timeout" in airplay:
        cfg.airplay_ready_timeout = float(airplay["ready_timeout"])
    if "target_latency_ms" in airplay:
        cfg.airplay_target_latency_ms = int(airplay["target_latency_ms"])
    if "audio" in airplay:
        cfg.airplay_audio = bool(airplay["audio"])

    cast = data.get("cast", {})
    if "http_port" in cast:
        cfg.cast_http_port = int(cast["http_port"])
    if "bitrate" in cast:
        cfg.cast_bitrate = int(cast["bitrate"])

    if cfg.encoder not in ENCODERS:
        raise ValueError(f"invalid encoder: {cfg.encoder}; expected one of {ENCODERS}")
    return cfg
