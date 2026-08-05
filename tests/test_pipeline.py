from omarchy_cast.capture.pipeline import build_pipeline_description
from omarchy_cast.core.config import Config


def test_pipeline_includes_node_id_and_fd():
    desc = build_pipeline_description(42, 7, "vaapi", Config())
    assert "pipewiresrc" in desc
    assert "path=42" in desc
    assert "fd=7" in desc


def test_pipeline_uses_selected_encoder_element():
    assert "vah264enc" in build_pipeline_description(1, 2, "vaapi", Config())
    assert "x264enc" in build_pipeline_description(1, 2, "x264", Config())
    assert "nvh264enc" in build_pipeline_description(1, 2, "nvenc", Config())


def test_pipeline_sets_zero_latency_for_x264():
    assert "tune=zerolatency" in build_pipeline_description(1, 2, "x264", Config())


def test_every_encoder_sets_an_explicit_bitrate():
    """Without this, vah264enc auto-calculated ~21 Mbps on real hardware."""
    for encoder in ("vaapi", "nvenc", "x264"):
        desc = build_pipeline_description(1, 2, encoder, Config(cast_bitrate=4000))
        assert "bitrate=4000" in desc


def test_every_encoder_bounds_the_gop():
    """A receiver joining mid-stream needs a keyframe within ~1s."""
    for encoder, key in (
        ("vaapi", "key-int-max"),
        ("nvenc", "gop-size"),
        ("x264", "key-int-max"),
    ):
        desc = build_pipeline_description(1, 2, encoder, Config(fps=30))
        assert f"{key}=30" in desc


def test_pipeline_ends_in_appsink():
    desc = build_pipeline_description(1, 2, "vaapi", Config())
    assert desc.strip().endswith("name=sink")
    assert "appsink" in desc


def test_pipeline_muxes_streamable_matroska():
    desc = build_pipeline_description(1, 2, "vaapi", Config())
    assert "matroskamux" in desc
    assert "streamable=true" in desc


def test_pipeline_honours_configured_fps():
    assert "framerate=60/1" in build_pipeline_description(1, 2, "vaapi", Config(fps=60))


def test_h264parse_repeats_headers():
    """Without config-interval a late joiner never sees SPS/PPS."""
    assert "config-interval=1" in build_pipeline_description(1, 2, "vaapi", Config())


def test_matches_the_verified_prototype_shape():
    """This exact pipeline produced 197 decodable frames at 2560x1600."""
    desc = build_pipeline_description(72, 8, "vaapi", Config())
    order = ["pipewiresrc", "videorate", "videoconvert", "vah264enc",
             "h264parse", "matroskamux", "appsink"]
    positions = [desc.index(part) for part in order]
    assert positions == sorted(positions)
