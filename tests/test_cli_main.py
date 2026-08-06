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
    assert calls[0] == ("start", {"device_id": "cast:1", "mode": "mirror"})


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
    assert calls[1] == ("start", {"device_id": "airplay:192.168.1.231", "mode": "mirror"})


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


# -- _run_menu: mode prompt, notifications --------------------------------


@pytest.fixture
def fake_menu(monkeypatch):
    """Stubs the three things _run_menu shells out to: the daemon socket,
    walker, and notify-send -- none of which exist in the test environment.
    """
    responses = {}
    walker_answers = {}
    notifications = []

    async def _request(cmd, path=None, **kwargs):
        return responses[cmd]

    def _walker(entries, prompt):
        return walker_answers.get(prompt, "")

    def _notify(message, urgent=False):
        notifications.append(message)

    monkeypatch.setattr(cli_main, "request", _request)
    monkeypatch.setattr(cli_main, "_walker", _walker)
    monkeypatch.setattr(cli_main, "_notify", _notify)
    return responses, walker_answers, notifications


def test_extend_notification_includes_warning_and_portal_hint(fake_menu):
    """The Chromecast-untested warning is sent on every cast-protocol start,
    mirror or extend -- it must not silently replace the extend hint that
    names the portal output to pick.
    """
    responses, walker_answers, notifications = fake_menu
    responses["list"] = {
        "ok": True,
        "data": {"devices": [{"id": "cast:1", "name": "Living Room", "protocol": "cast"}]},
    }
    responses["status"] = {"ok": True, "data": {"sessions": []}}
    responses["start"] = {
        "ok": True,
        "data": {"state": "streaming", "warning": "Chromecast support is UNTESTED"},
    }
    walker_answers["Cast to"] = "Living Room (Chromecast) [cast:1]"
    walker_answers["Mirror or extend?"] = (
        "Extend — second display (pick 'omarchy-cast' if the portal asks)"
    )

    assert cli_main._run_menu() == 0
    assert len(notifications) == 1
    assert "UNTESTED" in notifications[0]
    assert "omarchy-cast" in notifications[0]


def test_mirror_notification_shows_warning_alone(fake_menu):
    """Sanity check the other side of the same branch: mirror has no portal
    hint to compose in, so the warning alone is still delivered."""
    responses, walker_answers, notifications = fake_menu
    responses["list"] = {
        "ok": True,
        "data": {"devices": [{"id": "cast:1", "name": "Living Room", "protocol": "cast"}]},
    }
    responses["status"] = {"ok": True, "data": {"sessions": []}}
    responses["start"] = {
        "ok": True,
        "data": {"state": "streaming", "warning": "Chromecast support is UNTESTED"},
    }
    walker_answers["Cast to"] = "Living Room (Chromecast) [cast:1]"
    walker_answers["Mirror or extend?"] = "Mirror — show this screen on the receiver"

    assert cli_main._run_menu() == 0
    assert notifications == ["Chromecast support is UNTESTED"]


def test_manual_entry_skips_add_when_mode_prompt_cancelled(fake_menu, monkeypatch):
    """Mode is now asked before the device is registered, so cancelling the
    mode prompt during manual entry must not leave an orphaned device."""
    responses, walker_answers, notifications = fake_menu
    responses["list"] = {"ok": True, "data": {"devices": []}}
    responses["status"] = {"ok": True, "data": {"sessions": []}}
    add_calls = []

    async def _request(cmd, path=None, **kwargs):
        if cmd == "add":
            add_calls.append(kwargs)
        return responses[cmd]

    monkeypatch.setattr(cli_main, "request", _request)
    walker_answers["Cast to"] = cli_main.MANUAL_ENTRY
    walker_answers["Receiver IP address"] = "10.0.0.5"
    walker_answers["Mirror or extend?"] = ""  # cancelled

    assert cli_main._run_menu() == 0
    assert add_calls == []
