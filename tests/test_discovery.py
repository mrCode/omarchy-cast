from omarchy_cast.core.discovery import (
    AIRPLAY_TYPE,
    CAST_TYPE,
    device_from_airplay,
    device_from_cast,
)


class FakeInfo:
    def __init__(self, name, port, properties, addresses):
        self.name = name
        self.port = port
        self.properties = properties
        self._addresses = addresses

    def parsed_addresses(self):
        return self._addresses


def test_airplay_device_parsed():
    info = FakeInfo(
        name="Living Room._airplay._tcp.local.",
        port=7000,
        properties={b"deviceid": b"AA:BB:CC:DD:EE:FF", b"model": b"AppleTV14,1"},
        addresses=["192.168.1.77"],
    )
    d = device_from_airplay(info)
    assert d.id == "airplay:AA:BB:CC:DD:EE:FF"
    assert d.name == "Living Room"
    assert d.address == "192.168.1.77"
    assert d.port == 7000
    assert d.protocol == "airplay"
    assert d.model == "AppleTV14,1"


def test_cast_device_parsed():
    info = FakeInfo(
        name="Chromecast-abc._googlecast._tcp.local.",
        port=8009,
        properties={b"id": b"abc123", b"fn": b"Bedroom TV", b"md": b"Chromecast"},
        addresses=["192.168.1.50"],
    )
    d = device_from_cast(info)
    assert d.id == "cast:abc123"
    assert d.name == "Bedroom TV"
    assert d.address == "192.168.1.50"
    assert d.protocol == "cast"
    assert d.model == "Chromecast"


def test_device_without_address_is_skipped():
    info = FakeInfo("X._airplay._tcp.local.", 7000, {b"deviceid": b"A"}, [])
    assert device_from_airplay(info) is None


def test_airplay_without_deviceid_falls_back_to_name():
    info = FakeInfo("Studio._airplay._tcp.local.", 7000, {}, ["10.0.0.2"])
    d = device_from_airplay(info)
    assert d.id == "airplay:Studio"


def test_cast_without_friendly_name_falls_back_to_service_name():
    info = FakeInfo("Chromecast-xyz._googlecast._tcp.local.", 8009, {b"id": b"xyz"}, ["10.0.0.3"])
    d = device_from_cast(info)
    assert d.name == "Chromecast-xyz"


def test_ipv6_only_device_is_skipped():
    """Devices advertise link-local v6 alongside v4; we require a usable v4."""
    info = FakeInfo("X._googlecast._tcp.local.", 8009, {b"id": b"q"}, ["fe80::1"])
    assert device_from_cast(info) is None


def test_ipv4_chosen_when_v6_listed_first():
    info = FakeInfo(
        "living room-2._airplay._tcp.local.",
        7000,
        {b"deviceid": b"AA:BB:CC:DD:EE:01", b"model": b"AppleTV14,1"},
        ["fe80::1", "192.168.1.226"],
    )
    d = device_from_airplay(info)
    assert d.address == "192.168.1.226"


def test_unicode_names_survive():
    """Real networks are full of names like 'a name with an apostrophe'."""
    info = FakeInfo(
        "Zoë’s MacBook Air._airplay._tcp.local.",
        7000,
        {b"deviceid": b"AA:BB:CC:DD:EE:02", b"model": b"Mac14,2"},
        ["192.168.1.121"],
    )
    d = device_from_airplay(info)
    assert d.name == "Zoë’s MacBook Air"


def test_malformed_property_bytes_do_not_crash():
    info = FakeInfo(
        "X._googlecast._tcp.local.",
        8009,
        {b"id": b"\xff\xfe", b"fn": b"TV"},
        ["10.0.0.4"],
    )
    assert device_from_cast(info) is not None


def test_service_type_constants():
    assert AIRPLAY_TYPE == "_airplay._tcp.local."
    assert CAST_TYPE == "_googlecast._tcp.local."
