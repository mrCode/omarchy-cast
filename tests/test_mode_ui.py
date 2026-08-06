from omarchy_cast.cli.menu import MODE_ENTRIES, parse_mode
from omarchy_cast.cli.waybar import render


def test_mode_entries_offer_both():
    assert len(MODE_ENTRIES) == 2
    assert any("Mirror" in e for e in MODE_ENTRIES)
    assert any("Extend" in e for e in MODE_ENTRIES)


def test_parse_mode_round_trips():
    modes = {parse_mode(e) for e in MODE_ENTRIES}
    assert modes == {"mirror", "extend"}


def test_parse_mode_rejects_noise():
    assert parse_mode("") is None
    assert parse_mode("something else") is None


def test_extend_entry_names_the_output_to_pick():
    """Choosing the wrong output at the portal prompt silently mirrors."""
    extend = next(e for e in MODE_ENTRIES if "Extend" in e)
    assert "omarchy-cast" in extend


def test_waybar_tooltip_shows_the_mode():
    out = render([{
        "name": "Living Room", "protocol": "airplay",
        "state": "streaming", "mode": "extend", "error": None,
    }])
    assert "extend" in out["tooltip"].lower()


def test_waybar_tooltip_without_a_mode_still_works():
    """Older daemons omit the field; the indicator must not crash."""
    out = render([{
        "name": "Living Room", "protocol": "airplay",
        "state": "streaming", "error": None,
    }])
    assert "Living Room" in out["tooltip"]
