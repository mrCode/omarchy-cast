import shutil
import subprocess
from collections.abc import Callable

from omarchy_cast.core.config import Config

# Element names verified present via gst-inspect-1.0 on the target hardware.
ENCODER_ELEMENTS = {
    "vaapi": "vah264enc",
    "nvenc": "nvh264enc",
    "x264": "x264enc",
}


class NoEncoderAvailable(Exception):
    pass


def gst_element_for(encoder: str) -> str:
    return ENCODER_ELEMENTS[encoder]


def gst_inspect_runner(element: str) -> bool:
    """Real probe: gst-inspect-1.0 exits non-zero for unknown elements."""
    binary = shutil.which("gst-inspect-1.0")
    if binary is None:
        return False
    result = subprocess.run(
        [binary, element],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def probe_available(runner: Callable[[str], bool] = gst_inspect_runner) -> set[str]:
    return {key for key, element in ENCODER_ELEMENTS.items() if runner(element)}


def select_encoder(config: Config, available: set[str]) -> str:
    if config.encoder != "auto":
        if config.encoder not in available:
            raise NoEncoderAvailable(
                f"configured encoder {config.encoder!r} is not available; "
                f"found: {sorted(available) or 'none'}"
            )
        return config.encoder

    for candidate in config.encoder_ranking:
        if candidate in available:
            return candidate

    raise NoEncoderAvailable(
        "no H.264 encoder found; install gst-plugin-va (Intel/AMD) or "
        "gst-plugins-ugly (software x264)"
    )
