"""Parsing avahi-browse output. Fixtures are REAL captured lines, because the
format held a trap: the `proto` field says IPv6 while carrying an IPv4
address, and filtering on it drops every device."""

from omarchy_cast.core import avahi

APPLE_TV = (
    '=;wlan0;IPv6;Meeting\\032Room;AirPlay Remote Video;local;'
    'Meeting-Room.local;10.10.10.231;7000;'
    '"acl=0" "deviceid=FE:5C:81:22:50:38" "model=AppleTV11,1" "srcvers=960.13.1"'
)
MACBOOK = (
    '=;wlan0;IPv6;Ibrahim\\226\\128\\153s\\032MacBook\\032Air;AirPlay Remote Video;'
    'local;Ibrahims-MacBook-Air.local;10.10.10.224;7000;'
    '"deviceid=CE:65:83:54:F4:01" "model=MacBookAir10,1"'
)


def test_an_ipv6_transport_line_with_an_ipv4_address_is_kept():
    """The trap: avahi reports the mDNS transport in that field, not the
    address family. Rejecting 'IPv6' lines found nothing at all."""
    devices = avahi.parse(APPLE_TV, "airplay")

    assert len(devices) == 1
    assert devices[0].address == "10.10.10.231"


def test_fields_are_extracted():
    d = avahi.parse(APPLE_TV, "airplay")[0]

    assert d.id == "airplay:FE:5C:81:22:50:38"
    assert d.name == "Meeting Room"
    assert d.port == 7000
    assert d.model == "AppleTV11,1"
    assert d.protocol == "airplay"


def test_escaped_names_are_decoded():
    """Raw, this reads `Ibrahim\\226\\128\\153s\\032MacBook\\032Air` in the menu."""
    d = avahi.parse(MACBOOK, "airplay")[0]

    assert d.name == "Ibrahim’s MacBook Air"


def test_several_devices():
    devices = avahi.parse(f"{APPLE_TV}\n{MACBOOK}", "airplay")

    assert {d.name for d in devices} == {"Meeting Room", "Ibrahim’s MacBook Air"}


def test_unresolved_and_junk_lines_are_ignored():
    noise = "+;wlan0;IPv6;Something;AirPlay Remote Video;local\nrubbish\n\n"

    assert avahi.parse(noise + APPLE_TV, "airplay") == avahi.parse(APPLE_TV, "airplay")


def test_an_ipv6_only_receiver_is_skipped():
    """A Xiaomi box advertised link-local v6 only. Not a usable cast target."""
    line = (
        '=;wlan0;IPv6;MiTV;_googlecast._tcp;local;mitv.local;'
        'fe80::6153:933a:c814:99a4;8009;"id=abc" "md=Xiaomi"'
    )

    assert avahi.parse(line, "cast") == []


def test_a_device_with_no_id_falls_back_to_its_address():
    line = (
        '=;wlan0;IPv4;Plain;AirPlay Remote Video;local;plain.local;'
        '10.0.0.7;7000;"model=Thing"'
    )

    assert avahi.parse(line, "cast")[0].id == "cast:10.0.0.7"


def test_cast_uses_its_own_txt_keys():
    line = (
        '=;wlan0;IPv4;chromecast-abc;_googlecast._tcp;local;abc.local;'
        '10.0.0.8;8009;"id=deadbeef" "md=Chromecast Ultra" "fn=Living Room TV"'
    )

    d = avahi.parse(line, "cast")[0]

    assert d.id == "cast:deadbeef"
    assert d.name == "Living Room TV"
    assert d.model == "Chromecast Ultra"


# -- the Discovery surface --------------------------------------------------


def fake_runner(output, code=0):
    return lambda argv: (code, output if "_airplay" in argv[-1] else "")


def test_manual_entries_are_hidden_once_discovered():
    d = avahi.AvahiDiscovery(runner=fake_runner(APPLE_TV))
    from omarchy_cast.core.device import Device
    d.add(Device(id="airplay:10.10.10.231", name="Manual",
                 address="10.10.10.231", port=7000, protocol="airplay"))

    ids = [x.id for x in d.devices()]

    assert ids == ["airplay:FE:5C:81:22:50:38"]


def test_manual_entries_survive_when_not_discovered():
    d = avahi.AvahiDiscovery(runner=fake_runner(""))
    from omarchy_cast.core.device import Device
    d.add(Device(id="airplay:10.0.0.99", name="Manual", address="10.0.0.99",
                 port=7000, protocol="airplay"))

    assert [x.id for x in d.devices()] == ["airplay:10.0.0.99"]


def test_a_failing_avahi_yields_nothing_rather_than_raising():
    d = avahi.AvahiDiscovery(runner=lambda argv: (127, ""))

    assert d.devices() == []
    assert d.has_discovered() is False
