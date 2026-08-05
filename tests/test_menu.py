from omarchy_cast.cli.menu import MANUAL_ENTRY, format_entries, parse_selection


def test_entries_are_grouped_and_labelled():
    entries = format_entries([
        {"id": "cast:1", "name": "Bedroom", "protocol": "cast", "model": "Chromecast"},
        {"id": "airplay:2", "name": "Living Room", "protocol": "airplay", "model": "AppleTV14,1"},
    ])
    assert any("Living Room" in e and "airplay:2" in e for e in entries)
    assert any("Bedroom" in e and "cast:1" in e for e in entries)


def test_airplay_sorts_before_cast():
    entries = format_entries([
        {"id": "cast:1", "name": "AAA", "protocol": "cast", "model": None},
        {"id": "airplay:2", "name": "ZZZ", "protocol": "airplay", "model": None},
    ])
    assert "ZZZ" in entries[0]


def test_selection_round_trips_to_device_id():
    entries = format_entries([{"id": "cast:1", "name": "Bedroom", "protocol": "cast", "model": None}])
    assert parse_selection(entries[0]) == "cast:1"


def test_empty_selection_returns_none():
    assert parse_selection("") is None
    assert parse_selection("   ") is None


def test_garbage_selection_returns_none():
    assert parse_selection("no id here") is None


def test_manual_entry_offered_last():
    """mDNS fails on some networks, so entering an address must be reachable."""
    entries = format_entries([{"id": "cast:1", "name": "A", "protocol": "cast", "model": None}])
    assert entries[-1] == MANUAL_ENTRY


def test_manual_entry_present_even_with_no_devices():
    assert format_entries([]) == [MANUAL_ENTRY]


def test_manual_entry_parses_to_none_not_a_device():
    assert parse_selection(MANUAL_ENTRY) is None


def test_ids_with_colons_round_trip():
    """AirPlay ids embed a MAC, which is full of colons."""
    entries = format_entries([
        {"id": "airplay:AA:BB:CC:DD:EE:01", "name": "Living Room", "protocol": "airplay", "model": None}
    ])
    assert parse_selection(entries[0]) == "airplay:AA:BB:CC:DD:EE:01"


def test_unicode_names_survive():
    entries = format_entries([
        {"id": "airplay:1", "name": "Zoë’s MacBook Air", "protocol": "airplay", "model": "Mac14,2"}
    ])
    assert "Zoë’s MacBook Air" in entries[0]
