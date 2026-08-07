from omarchy_cast.capture.pipeline import build_pipeline_description
from omarchy_cast.core.config import Config


def test_pipeline_includes_node_id_and_fd():
    """`path=` was asserted here until it turned out GStreamer 1.28 ignores it
    -- this test passed while the pipeline captured the webcam instead."""
    desc = build_pipeline_description(42, 7, "vaapi", Config())
    assert "pipewiresrc" in desc
    assert "target-object=42" in desc
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
    """As a videorate property, not a capsfilter -- see the negotiation tests
    below for why the capsfilter form produced no video at all."""
    assert "max-rate=60" in build_pipeline_description(1, 2, "vaapi", Config(fps=60))


def test_h264parse_repeats_headers():
    """Without config-interval a late joiner never sees SPS/PPS."""
    assert "config-interval=1" in build_pipeline_description(1, 2, "vaapi", Config())


def test_element_order():
    """Previously asserted a "verified prototype shape" with videorate ahead of
    videoconvert. That shape produced 0 buffers in production -- the prototype
    it was copied from evidently differed. Measured against a live capture, the
    order below yields ~45 chunks in five seconds."""
    desc = build_pipeline_description(72, 8, "vaapi", Config())
    order = ["pipewiresrc", "videoconvert", "videorate", "vah264enc",
             "h264parse", "matroskamux", "appsink"]
    positions = [desc.index(part) for part in order]
    assert positions == sorted(positions)


# -- the capsfilter that stopped all video ----------------------------------


def test_the_source_is_not_constrained_by_a_framerate_capsfilter():
    """A `video/x-raw,framerate=N/1` capsfilter makes pipewiresrc fail
    negotiation with `set output format: -22` and produce no buffers at all,
    silently -- while the session still reports STREAMING. Measured on a live
    Hyprland capture: 0 chunks with it, ~45 chunks without."""
    desc = build_pipeline_description(79, 11, "x264", Config())

    assert "video/x-raw,framerate" not in desc
    assert "video/x-raw" not in desc


def test_the_framerate_is_still_applied_as_a_property():
    """Dropping the capsfilter must not silently drop the fps setting."""
    desc = build_pipeline_description(79, 11, "x264", Config(fps=24))

    assert "videorate max-rate=24" in desc


def test_videorate_runs_after_videoconvert():
    """Ordering matters: before videoconvert it constrains the source pad,
    which is the negotiation failure all over again."""
    desc = build_pipeline_description(79, 11, "x264", Config())

    assert desc.index("videoconvert") < desc.index("videorate")


# -- SECURITY: the source must never be substitutable ------------------------


def test_autoconnect_is_disabled():
    """With autoconnect on, pipewiresrc "finds a peer" when it cannot use the
    requested node -- and it picked the built-in WEBCAM, which was then encoded
    and streamed to a television while the app reported screen mirroring.
    Verified by PipeWire link tracing: portal gave node 87, pipeline linked to
    node 60 with media.role=Camera."""
    desc = build_pipeline_description(87, 11, "x264", Config())

    assert "autoconnect=false" in desc


def test_the_node_is_selected_with_target_object_not_the_deprecated_path():
    """`path` is deprecated in GStreamer 1.28 and silently ignored, which is
    how the requested node came to be disregarded in the first place."""
    desc = build_pipeline_description(87, 11, "x264", Config())

    assert "target-object=87" in desc
    assert "path=" not in desc


def test_the_portal_fd_is_still_passed():
    """Without it the source is not scoped to the portal session at all."""
    assert "fd=11" in build_pipeline_description(87, 11, "x264", Config())
