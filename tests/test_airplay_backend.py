import json

import pytest

from omarchy_cast.backends.airplay import AirPlayBackend
from omarchy_cast.backends.base import BackendError
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


def make_device():
    return Device(
        id="airplay:AA", name="Living Room", address="192.168.1.77",
        port=7000, protocol="airplay",
    )


def envelope(state="idle", ok=True, **extra):
    """A doubletake-ctl JSON envelope, matching the real binary's output."""
    return json.dumps({
        "ok": ok,
        "state": state,
        "has_audio": False,
        "audio_muted": False,
        **extra,
    })


class FakeRunner:
    """Fakes doubletake-ctl. `status_sequence` is consumed one entry per poll."""

    def __init__(self, results=None, status_sequence=None):
        self.calls = []
        self.results = results or {}
        self.status_sequence = list(status_sequence or [])

    async def __call__(self, argv):
        self.calls.append(argv)
        if "status" in argv and self.status_sequence:
            return (0, self.status_sequence.pop(0), "")
        for key, result in self.results.items():
            if key in argv:
                return result
        return (0, envelope(), "")


def make_backend(runner, connect_timeout=5.0, **cfg):
    states = []
    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(**cfg),
        runner=runner,
        poll_interval=0.0,
        connect_timeout=connect_timeout,
    )
    return backend, states


async def test_start_launches_daemon_then_connects():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("connecting"), envelope("streaming")],
    )
    backend, states = make_backend(runner, airplay_port_range="60000-60010")
    await backend.start(make_device())

    joined = [" ".join(c) for c in runner.calls]
    assert any("-daemonize" in c and "60000-60010" in c for c in joined)
    assert any("connect 192.168.1.77" in c for c in joined)
    assert states[0][0] is SessionState.CONNECTING
    assert states[-1][0] is SessionState.STREAMING


async def test_start_polls_until_streaming():
    """connect returns 'connecting' immediately; success is only known by polling."""
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[
            envelope("connecting"),
            envelope("connecting"),
            envelope("streaming"),
        ],
    )
    backend, states = make_backend(runner)
    await backend.start(make_device())
    assert len([c for c in runner.calls if "status" in c]) == 3
    assert states[-1][0] is SessionState.STREAMING


async def test_daemon_started_only_once():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("streaming"), envelope("streaming")],
    )
    backend, _ = make_backend(runner)
    await backend.start(make_device())
    await backend.start(make_device())
    assert len([c for c in runner.calls if "-daemonize" in c]) == 1


async def test_pin_required_enters_awaiting_pin():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("pin_required", needs_pin=True)],
    )
    backend, states = make_backend(runner)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.AWAITING_PIN


async def test_submit_pin_reaches_streaming():
    runner = FakeRunner(
        results={
            "connect": (0, envelope("connecting"), ""),
            "pin": (0, envelope("streaming"), ""),
        },
        status_sequence=[envelope("pin_required", needs_pin=True)],
    )
    backend, states = make_backend(runner)
    device = make_device()
    await backend.start(device)
    await backend.submit_pin(device, "4029")
    assert any(c[-2:] == ["pin", "4029"] for c in runner.calls)
    assert states[-1][0] is SessionState.STREAMING


async def test_connect_error_envelope_is_actionable():
    """Failure arrives as ok:false with an error string, not a failed state."""
    runner = FakeRunner(
        results={"connect": (0, envelope("idle", ok=False, error="connection timed out"), "")}
    )
    backend, states = make_backend(runner)
    with pytest.raises(BackendError, match="firewall"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED


async def test_connect_timeout_mentions_firewall():
    """Never reaching streaming is the documented silent-stall firewall case."""
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("connecting")] * 500,
    )
    backend, states = make_backend(runner, connect_timeout=0.05)
    with pytest.raises(BackendError, match="firewall"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED


async def test_password_protected_receiver_is_named_explicitly():
    """A 401 means Require Password is on; upstream doubletake #26 covers it.

    Verified against a real AppleTV11,1 that failed exactly this way.
    """
    runner = FakeRunner(
        results={
            "connect": (
                0,
                envelope("idle", ok=False,
                         error="mirror setup failed: SETUP phase 1 (audio): HTTP 401 (body: )"),
                "",
            )
        }
    )
    backend, states = make_backend(runner)
    with pytest.raises(BackendError) as excinfo:
        await backend.start(make_device())
    message = str(excinfo.value)
    assert "Require Password" in message
    assert "#26" in message
    assert states[-1][0] is SessionState.FAILED


async def test_malformed_output_is_actionable():
    runner = FakeRunner(results={"connect": (0, "not json at all", "")})
    backend, _ = make_backend(runner)
    with pytest.raises(BackendError, match="unexpected output"):
        await backend.start(make_device())


async def test_stop_disconnects_specific_device():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("streaming")],
    )
    backend, states = make_backend(runner)
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert any("disconnect 192.168.1.77" in " ".join(c) for c in runner.calls)
    assert states[-1][0] is SessionState.IDLE


async def test_missing_doubletake_binary_is_actionable():
    async def runner(argv):
        raise FileNotFoundError(argv[0])

    backend, _ = make_backend(runner)
    with pytest.raises(BackendError, match="doubletake"):
        await backend.start(make_device())


async def test_configured_code_is_passed_in_the_environment():
    """Plumbed ahead of upstream #26 landing, so support is a config change."""
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("streaming")],
    )
    backend, _ = make_backend(runner, airplay_code="1234")
    assert backend.daemon_env().get("DOUBLETAKE_CODE") == "1234"


async def test_no_code_means_no_env_var():
    runner = FakeRunner()
    backend, _ = make_backend(runner)
    assert "DOUBLETAKE_CODE" not in backend.daemon_env()


async def test_shutdown_disconnects_everything():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("streaming")],
    )
    backend, _ = make_backend(runner)
    await backend.start(make_device())
    await backend.shutdown()
    assert any(c == ["doubletake-ctl", "disconnect"] for c in runner.calls)
