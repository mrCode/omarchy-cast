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
    cast_http_port: int = 0
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

    cast = data.get("cast", {})
    if "http_port" in cast:
        cfg.cast_http_port = int(cast["http_port"])
    if "bitrate" in cast:
        cfg.cast_bitrate = int(cast["bitrate"])

    if cfg.encoder not in ENCODERS:
        raise ValueError(f"invalid encoder: {cfg.encoder}; expected one of {ENCODERS}")
    return cfg
