from omarchy_cast.cli.waybar import render


def test_idle_state_is_visible_not_blank():
    """Toggle indicators stay visible in both states; colour carries meaning."""
    out = render([])
    assert out["text"] != ""
    assert out["class"] == "idle"
    assert "not casting" in out["tooltip"].lower()


def test_streaming_shows_device_name():
    out = render([{"name": "Living Room", "protocol": "airplay", "state": "streaming", "error": None}])
    assert "Living Room" in out["tooltip"]
    assert out["class"] == "streaming"


def test_failed_state_surfaces_error():
    out = render([{"name": "TV", "protocol": "cast", "state": "failed", "error": "firewall blocked"}])
    assert out["class"] == "failed"
    assert "firewall blocked" in out["tooltip"]


def test_failed_without_message_still_readable():
    out = render([{"name": "TV", "protocol": "cast", "state": "failed", "error": None}])
    assert out["class"] == "failed"
    assert out["tooltip"]


def test_multiple_sessions_counted():
    out = render([
        {"name": "A", "protocol": "cast", "state": "streaming", "error": None},
        {"name": "B", "protocol": "airplay", "state": "streaming", "error": None},
    ])
    assert "2" in out["text"]


def test_connecting_is_its_own_class():
    out = render([{"name": "A", "protocol": "cast", "state": "connecting", "error": None}])
    assert out["class"] == "connecting"


def test_awaiting_pin_reads_as_connecting_and_prompts():
    out = render([{"name": "A", "protocol": "airplay", "state": "awaiting_pin", "error": None}])
    assert out["class"] == "connecting"
    assert "pin" in out["tooltip"].lower()


def test_failure_wins_over_streaming():
    out = render([
        {"name": "A", "protocol": "cast", "state": "streaming", "error": None},
        {"name": "B", "protocol": "airplay", "state": "failed", "error": "boom"},
    ])
    assert out["class"] == "failed"


def test_all_states_produce_the_three_required_keys():
    for state in ("idle", "connecting", "awaiting_pin", "streaming", "failed"):
        sessions = [] if state == "idle" else [
            {"name": "X", "protocol": "cast", "state": state, "error": "e"}
        ]
        out = render(sessions)
        assert set(out) == {"text", "tooltip", "class"}
