import pytest

from omarchy_cast.cli import main as cli_main


@pytest.fixture
def fake_request(monkeypatch):
    calls = []
    responses = {}

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
        return responses[cmd]

    monkeypatch.setattr(cli_main, "request", _request)
    return calls, responses


def test_list_prints_devices(fake_request, capsys):
    calls, responses = fake_request
    responses["list"] = {
        "ok": True,
        "data": {
            "devices": [
                {"id": "cast:1", "name": "TV", "protocol": "cast", "model": "Chromecast"}
            ]
        },
    }
    assert cli_main.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "cast:1" in out and "TV" in out


def test_list_empty_is_not_silent(fake_request, capsys):
    calls, responses = fake_request
    responses["list"] = {"ok": True, "data": {"devices": []}}
    assert cli_main.main(["list"]) == 0
    assert "no receivers" in capsys.readouterr().out.lower()


def test_start_passes_device_id(fake_request):
    calls, responses = fake_request
    responses["start"] = {"ok": True, "data": {"state": "streaming"}}
    assert cli_main.main(["start", "cast:1"]) == 0
    assert calls[0] == ("start", {"device_id": "cast:1"})


def test_error_response_returns_nonzero_and_prints_to_stderr(fake_request, capsys):
    calls, responses = fake_request
    responses["start"] = {"ok": False, "error": "firewall blocked"}
    assert cli_main.main(["start", "cast:1"]) == 1
    assert "firewall blocked" in capsys.readouterr().err


def test_stop_without_device_sends_no_device_id(fake_request):
    calls, responses = fake_request
    responses["stop"] = {"ok": True, "data": {"stopped": 1}}
    assert cli_main.main(["stop"]) == 0
    assert calls[0] == ("stop", {"device_id": None})


def test_status_not_casting(fake_request, capsys):
    calls, responses = fake_request
    responses["status"] = {"ok": True, "data": {"sessions": []}}
    assert cli_main.main(["status"]) == 0
    assert "not casting" in capsys.readouterr().out.lower()


def test_pin_passes_both_args(fake_request):
    calls, responses = fake_request
    responses["pin"] = {"ok": True, "data": {"state": "streaming"}}
    assert cli_main.main(["pin", "airplay:x", "4029"]) == 0
    assert calls[0] == ("pin", {"device_id": "airplay:x", "pin": "4029"})


def test_start_by_address_registers_then_starts(fake_request):
    """mDNS is unusable on some networks; --address must be first class."""
    calls, responses = fake_request
    responses["add"] = {
        "ok": True,
        "data": {"device": {"id": "airplay:192.168.1.231", "address": "192.168.1.231"}},
    }
    responses["start"] = {"ok": True, "data": {"state": "streaming"}}
    assert cli_main.main(["start", "--address", "192.168.1.231"]) == 0
    assert calls[0][0] == "add"
    assert calls[0][1]["address"] == "192.168.1.231"
    assert calls[1] == ("start", {"device_id": "airplay:192.168.1.231"})


def test_start_by_address_honours_protocol(fake_request):
    calls, responses = fake_request
    responses["add"] = {
        "ok": True,
        "data": {"device": {"id": "cast:10.0.0.9", "address": "10.0.0.9"}},
    }
    responses["start"] = {"ok": True, "data": {"state": "streaming"}}
    cli_main.main(["start", "--address", "10.0.0.9", "--protocol", "cast"])
    assert calls[0][1]["protocol"] == "cast"


def test_start_requires_device_or_address(capsys):
    with pytest.raises(SystemExit):
        cli_main.main(["start"])


def test_add_failure_stops_before_start(fake_request, capsys):
    calls, responses = fake_request
    responses["add"] = {"ok": False, "error": "unknown protocol: bogus"}
    assert cli_main.main(["start", "--address", "1.2.3.4"]) == 1
    assert len(calls) == 1
    assert "unknown protocol" in capsys.readouterr().err


def test_daemon_unavailable_returns_two(fake_request, capsys, monkeypatch):
    async def _boom(cmd, path=None, **kwargs):
        raise cli_main.DaemonUnavailable("no daemon socket")

    monkeypatch.setattr(cli_main, "request", _boom)
    assert cli_main.main(["list"]) == 2
    assert "no daemon socket" in capsys.readouterr().err
