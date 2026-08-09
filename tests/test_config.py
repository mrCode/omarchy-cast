from omarchy_cast.core.config import Config, load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg == Config()
    assert cfg.encoder_ranking == ["vaapi", "x264", "nvenc"]
    assert cfg.fps == 30


def test_file_overrides_only_named_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[capture]\nfps = 60\n")
    cfg = load_config(p)
    assert cfg.fps == 60
    assert cfg.encoder == "auto"


def test_rejects_unknown_encoder(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[capture]\nencoder = "quicksync"\n')
    try:
        load_config(p)
    except ValueError as e:
        assert "encoder" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_cast_bitrate_defaults_to_8000(tmp_path):
    """An explicit bitrate is mandatory; cbr without one measured 21.4 Mbps."""
    assert load_config(tmp_path / "missing.toml").cast_bitrate == 8000


def test_cast_section_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[cast]\nbitrate = 4000\nhttp_port = 9123\n")
    cfg = load_config(p)
    assert cfg.cast_bitrate == 4000
    assert cfg.cast_http_port == 9123


def test_airplay_section_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[airplay]\nport_range = "50000-50010"\nbitrate = 4500\ncode = "1234"\n')
    cfg = load_config(p)
    assert cfg.airplay_port_range == "50000-50010"
    assert cfg.airplay_bitrate == 4500
    assert cfg.airplay_code == "1234"


def test_airplay_code_defaults_empty(tmp_path):
    assert load_config(tmp_path / "missing.toml").airplay_code == ""


def test_encoder_ranking_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[capture]\nencoder_ranking = ["nvenc", "vaapi"]\n')
    assert load_config(p).encoder_ranking == ["nvenc", "vaapi"]


def test_explicit_valid_encoder_accepted(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[capture]\nencoder = "vaapi"\n')
    assert load_config(p).encoder == "vaapi"


def test_configs_do_not_share_ranking_list():
    """Mutable default must not leak between instances."""
    a, b = Config(), Config()
    a.encoder_ranking.append("bogus")
    assert "bogus" not in b.encoder_ranking


def test_cast_http_port_is_fixed_not_ephemeral(tmp_path):
    """The receiver connects INTO this port, so it must be firewallable.

    An ephemeral port lands in 32768-60999 and cannot be allowed through a
    firewall ahead of time, which silently breaks casting.
    """
    port = load_config(tmp_path / "missing.toml").cast_http_port
    assert port != 0
    assert not (32768 <= port <= 60999)


def test_ready_timeout_defaults_high_enough_for_a_real_receiver():
    """Measured on an AppleTV11,1: capture began 23s after 'mirror session
    ready', and extend adds a portal round-trip. At 30s extend timed out
    repeatedly while mirror just squeaked through."""
    assert Config().airplay_ready_timeout >= 60


def test_ready_timeout_is_configurable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[airplay]\nready_timeout = 90\n")

    assert load_config(path).airplay_ready_timeout == 90
