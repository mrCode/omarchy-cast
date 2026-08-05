import pytest

from omarchy_cast.capture.encoder import (
    ENCODER_ELEMENTS,
    NoEncoderAvailable,
    gst_element_for,
    probe_available,
    select_encoder,
)
from omarchy_cast.core.config import Config


def test_probe_uses_runner_per_element():
    seen = []

    def runner(element: str) -> bool:
        seen.append(element)
        return element == "x264enc"

    assert probe_available(runner) == {"x264"}
    assert set(seen) == {"vah264enc", "nvh264enc", "x264enc"}


def test_select_prefers_vaapi_over_nvenc_by_default():
    """NVENC is ranked last on purpose: the display runs off the iGPU, so NVENC
    forces a cross-GPU copy."""
    cfg = Config()
    assert select_encoder(cfg, {"vaapi", "nvenc", "x264"}) == "vaapi"


def test_select_falls_back_to_x264_before_nvenc():
    cfg = Config()
    assert select_encoder(cfg, {"nvenc", "x264"}) == "x264"


def test_explicit_encoder_is_honoured():
    cfg = Config(encoder="nvenc")
    assert select_encoder(cfg, {"vaapi", "nvenc"}) == "nvenc"


def test_explicit_encoder_missing_raises():
    cfg = Config(encoder="nvenc")
    with pytest.raises(NoEncoderAvailable, match="nvenc"):
        select_encoder(cfg, {"x264"})


def test_no_encoders_at_all_raises():
    with pytest.raises(NoEncoderAvailable):
        select_encoder(Config(), set())


def test_no_encoders_message_is_actionable():
    with pytest.raises(NoEncoderAvailable, match="gst-plugin-va"):
        select_encoder(Config(), set())


def test_custom_ranking_respected():
    cfg = Config(encoder_ranking=["nvenc", "vaapi", "x264"])
    assert select_encoder(cfg, {"vaapi", "nvenc"}) == "nvenc"


def test_ranking_entry_not_installed_is_skipped():
    cfg = Config(encoder_ranking=["nvenc", "x264"])
    assert select_encoder(cfg, {"vaapi", "x264"}) == "x264"


def test_gst_element_lookup():
    assert gst_element_for("vaapi") == "vah264enc"
    assert gst_element_for("nvenc") == "nvh264enc"
    assert gst_element_for("x264") == "x264enc"


def test_element_names_match_verified_hardware():
    """These exact names were confirmed present via gst-inspect-1.0."""
    assert ENCODER_ELEMENTS == {
        "vaapi": "vah264enc",
        "nvenc": "nvh264enc",
        "x264": "x264enc",
    }
