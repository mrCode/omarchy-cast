import json

import pytest

from omarchy_cast.core import manual
from omarchy_cast.core.device import Device


def make_device(ident="10.10.10.231", name="Meeting Room", protocol="airplay"):
    return Device(
        id=Device.make_id(protocol, ident),
        name=name,
        address=ident,
        port=7000,
        protocol=protocol,
    )


@pytest.fixture
def store(tmp_path):
    return tmp_path / "manual-devices.json"


# -- round trip -------------------------------------------------------------


def test_a_remembered_device_survives(store):
    """The whole point: the daemon exits after 30s idle, and a receiver that
    needed --address once will need it every time."""
    manual.remember(make_device(), store)

    devices = manual.load(store)

    assert [d.id for d in devices] == ["airplay:10.10.10.231"]
    assert devices[0].name == "Meeting Room"
    assert devices[0].port == 7000
    assert devices[0].protocol == "airplay"


def test_remembering_the_same_device_twice_does_not_duplicate_it(store):
    manual.remember(make_device(), store)
    manual.remember(make_device(name="Meeting Room (renamed)"), store)

    devices = manual.load(store)

    assert len(devices) == 1
    assert devices[0].name == "Meeting Room (renamed)"


def test_several_devices_are_kept(store):
    manual.remember(make_device(), store)
    manual.remember(make_device("10.10.10.43", "Muath's MacBook"), store)

    assert len(manual.load(store)) == 2


# -- forgetting -------------------------------------------------------------


def test_forget_removes_only_the_named_device(store):
    manual.remember(make_device(), store)
    manual.remember(make_device("10.10.10.43", "Muath's MacBook"), store)

    assert manual.forget("airplay:10.10.10.231", store) is True

    assert [d.id for d in manual.load(store)] == ["airplay:10.10.10.43"]


def test_forgetting_something_unknown_reports_it(store):
    """The daemon turns this into an error rather than a silent success."""
    manual.remember(make_device(), store)

    assert manual.forget("airplay:no-such-device", store) is False
    assert len(manual.load(store)) == 1


# -- a broken file must not break casting -----------------------------------


def test_a_missing_file_is_simply_empty(store):
    assert manual.load(store) == []


def test_corrupt_json_is_ignored_rather_than_fatal(store):
    """Losing remembered devices is a nuisance; a daemon that will not start
    means no casting at all."""
    store.write_text("{ this is not json")

    assert manual.load(store) == []


def test_a_json_object_instead_of_a_list_is_ignored(store):
    store.write_text('{"id": "airplay:1"}')

    assert manual.load(store) == []


def test_one_unusable_entry_does_not_discard_the_others(store):
    """A hand-edit or a protocol dropped in a later version costs only itself."""
    store.write_text(
        json.dumps(
            [
                {"id": "airplay:1", "name": "Good", "address": "10.0.0.1",
                 "port": 7000, "protocol": "airplay"},
                {"id": "broken", "name": "Bad", "protocol": "airplay"},  # no address
                {"id": "x:1", "name": "Wrong protocol", "address": "10.0.0.2",
                 "port": 1, "protocol": "smoke-signal"},
            ]
        )
    )

    devices = manual.load(store)

    assert [d.id for d in devices] == ["airplay:1"]


def test_save_reports_failure_instead_of_raising(store, monkeypatch):
    """Remembering is a convenience; failing to remember must not fail the add."""
    def boom(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(manual.os, "replace", boom)

    assert manual.save([make_device()], store) is False


def test_a_failed_save_leaves_no_temp_files_behind(store, monkeypatch):
    def boom(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(manual.os, "replace", boom)
    manual.save([make_device()], store)

    leftovers = [p.name for p in store.parent.iterdir()
                 if p.name.startswith(f".{manual.FILENAME}")]
    assert leftovers == []


def test_the_write_is_atomic(store):
    """The daemon can be killed at any moment; a half-written file would lose
    every remembered device, not just the one being added."""
    manual.remember(make_device(), store)
    manual.remember(make_device("10.10.10.43", "Second"), store)

    # A complete, parseable document after every write.
    assert len(json.loads(store.read_text())) == 2
