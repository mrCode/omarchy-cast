"""A receiver added by address must not appear twice once mDNS finds it."""

from omarchy_cast.core.device import Device
from omarchy_cast.core.discovery import Discovery


def manual_device(address="10.10.10.231", name="Meeting Room", protocol="airplay"):
    """What `--address` produces: id keyed on the address, no model."""
    return Device(
        id=Device.make_id(protocol, address),
        name=name,
        address=address,
        port=7000,
        protocol=protocol,
    )


def discovered_device(
    address="10.10.10.231", name="Meeting Room", protocol="airplay",
    ident="FE:5C:81:22:50:38", model="AppleTV11,1",
):
    """What mDNS produces: id keyed on the device id, and a model."""
    return Device(
        id=Device.make_id(protocol, ident),
        name=name,
        address=address,
        port=7000,
        protocol=protocol,
        model=model,
    )


def make_discovery():
    # No Zeroconf instance: nothing here browses the network.
    return Discovery(zeroconf=object())


def test_a_manual_entry_is_hidden_once_mdns_finds_the_same_receiver():
    """The menu listed one Apple TV twice -- once as its address, once as its
    real name and model -- because the two records carry different ids."""
    d = make_discovery()
    d.add(manual_device())
    d._devices[discovered_device().id] = discovered_device()

    devices = d.devices()

    assert len(devices) == 1
    # The discovered record wins: it has the model and an id that survives the
    # address changing under DHCP.
    assert devices[0].model == "AppleTV11,1"
    assert devices[0].id == "airplay:FE:5C:81:22:50:38"


def test_a_manual_entry_is_still_shown_when_mdns_has_not_found_it():
    """The whole reason --address exists: on one tested network the Apple TV
    answered no mDNS query at all."""
    d = make_discovery()
    d.add(manual_device())

    assert [x.id for x in d.devices()] == ["airplay:10.10.10.231"]


def test_the_manual_entry_comes_back_if_the_receiver_stops_advertising():
    """Discovery on the tested network was intermittent, so hiding must be a
    live view -- never deletion. Forgetting the fallback would strand the user
    exactly when they need it."""
    d = make_discovery()
    d.add(manual_device())
    d._devices[discovered_device().id] = discovered_device()
    assert len(d.devices()) == 1

    # mDNS loses it again.
    d.remove(discovered_device().id)

    assert [x.id for x in d.devices()] == ["airplay:10.10.10.231"]


def test_a_different_address_is_not_deduplicated():
    d = make_discovery()
    d.add(manual_device(address="10.10.10.231"))
    d._devices[discovered_device(address="10.10.10.43").id] = discovered_device(
        address="10.10.10.43", name="Muath's MacBook"
    )

    assert len(d.devices()) == 2


def test_the_same_address_on_a_different_protocol_is_not_deduplicated():
    """A Chromecast and an Apple TV really can share an address across a
    reassignment, and they are not the same receiver."""
    d = make_discovery()
    d.add(manual_device(protocol="cast"))
    d._devices[discovered_device().id] = discovered_device()

    assert len(d.devices()) == 2


def test_two_discovered_receivers_are_never_collapsed():
    """Only manual entries are folded away. Two mDNS records sharing an address
    is a situation we have no business resolving by guessing."""
    d = make_discovery()
    a = discovered_device(ident="AA:AA:AA:AA:AA:AA", name="One")
    b = discovered_device(ident="BB:BB:BB:BB:BB:BB", name="Two")
    d._devices[a.id] = a
    d._devices[b.id] = b

    assert len(d.devices()) == 2


def test_re_adding_the_same_manual_device_does_not_duplicate_it():
    d = make_discovery()
    d.add(manual_device())
    d.add(manual_device(name="Renamed"))

    devices = d.devices()

    assert len(devices) == 1
    assert devices[0].name == "Renamed"


# -- mDNS losing a receiver must not take the manual fallback with it -------


class FakeZeroconf:
    def get_service_info(self, *a, **kw):
        return None


def test_mdns_dropping_a_receiver_leaves_the_manual_entry_intact():
    """The removal path deletes by NAME, and a manual entry added for the same
    box carries the same name -- so losing the announcement deleted the very
    fallback that exists for when announcements stop arriving."""
    from zeroconf import ServiceStateChange

    from omarchy_cast.core.discovery import AIRPLAY_TYPE

    d = Discovery(zeroconf=FakeZeroconf())
    d.add(manual_device())
    found = discovered_device()
    d._devices[found.id] = found
    assert len(d.devices()) == 1

    d._on_change(
        FakeZeroconf(),
        AIRPLAY_TYPE,
        "Meeting Room._airplay._tcp.local.",
        ServiceStateChange.Removed,
    )

    assert [x.id for x in d.devices()] == ["airplay:10.10.10.231"]
