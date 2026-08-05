import pytest

from omarchy_cast.core.device import Device


def test_make_id_namespaces_by_protocol():
    assert Device.make_id("cast", "abc-123") == "cast:abc-123"
    assert Device.make_id("airplay", "AA:BB") == "airplay:AA:BB"


def test_device_is_frozen():
    d = Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")
    with pytest.raises(Exception):
        d.name = "other"


def test_device_rejects_unknown_protocol():
    with pytest.raises(ValueError, match="unknown protocol"):
        Device(id="x:1", name="TV", address="1.2.3.4", port=1, protocol="bogus")
