"""The TUI's presentation logic, kept out of the widgets so it is testable."""

from omarchy_cast.tui.model import (
    STATE_STYLE,
    device_row,
    merge,
    session_summary,
    should_prompt_for_pin,
)


def dev(id="airplay:1", name="Living Room", protocol="airplay", model="AppleTV14,1"):
    return {"id": id, "name": name, "protocol": protocol, "model": model, "address": "192.168.1.5"}


def sess(id="airplay:1", state="streaming", error=None, name="Living Room"):
    return {"id": id, "name": name, "protocol": "airplay", "state": state, "error": error}


def test_merge_marks_device_with_its_session_state():
    rows = merge([dev()], [sess()])
    assert rows[0]["state"] == "streaming"


def test_merge_leaves_idle_devices_alone():
    rows = merge([dev()], [])
    assert rows[0]["state"] == "idle"


def test_merge_includes_active_devices_not_in_discovery():
    """A device added by raw address may never appear in the discovery list."""
    rows = merge([], [sess(id="airplay:9", name="Manual")])
    assert len(rows) == 1
    assert rows[0]["id"] == "airplay:9"
    assert rows[0]["state"] == "streaming"


def test_merge_does_not_duplicate():
    rows = merge([dev()], [sess()])
    assert len(rows) == 1


def test_merge_sorts_airplay_first_then_name():
    rows = merge(
        [dev(id="cast:1", name="AAA", protocol="cast"), dev(id="airplay:2", name="ZZZ")],
        [],
    )
    assert [r["id"] for r in rows] == ["airplay:2", "cast:1"]


def test_device_row_renders_all_columns():
    name, proto, model, state = device_row({**dev(), "state": "streaming"})
    assert name == "Living Room"
    assert proto == "AirPlay"
    assert model == "AppleTV14,1"
    assert "streaming" in state


def test_device_row_handles_missing_model():
    _, _, model, _ = device_row({**dev(model=None), "state": "idle"})
    assert model == "-"


def test_every_state_has_a_style():
    for state in ("idle", "connecting", "awaiting_pin", "streaming", "stopping", "failed"):
        assert state in STATE_STYLE


def test_session_summary_when_idle():
    assert "Not casting" in session_summary([])


def test_session_summary_names_the_target():
    assert "Living Room" in session_summary([sess()])


def test_session_summary_surfaces_error():
    assert "boom" in session_summary([sess(state="failed", error="boom")])


def test_session_summary_counts_multiple():
    text = session_summary([sess(), sess(id="cast:2", name="TV")])
    assert "2" in text


def test_should_prompt_for_pin_only_when_awaiting():
    assert should_prompt_for_pin([sess(state="awaiting_pin")]) == "airplay:1"
    assert should_prompt_for_pin([sess(state="streaming")]) is None
    assert should_prompt_for_pin([]) is None
