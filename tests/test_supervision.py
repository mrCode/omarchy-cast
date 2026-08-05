"""The AirPlay backend must notice doubletake dying mid-stream.

Without this a crash 20 minutes into a session leaves the session STREAMING
forever: waybar keeps showing green and stop does nothing useful.
"""

import asyncio
import json

from omarchy_cast.backends.airplay import AirPlayBackend
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


def make_device():
    return Device(
        id="airplay:AA", name="Living Room", address="192.168.1.77",
        port=7000, protocol="airplay",
    )


def envelope(state="idle", ok=True, **extra):
    return json.dumps({"ok": ok, "state": state, "has_audio": False,
                       "audio_muted": False, **extra})


class ScriptedRunner:
    """Returns queued status envelopes, then repeats the last one forever."""

    def __init__(self, statuses):
        self.calls = []
        self.statuses = list(statuses)
        self._last = envelope("streaming")

    async def __call__(self, argv):
        self.calls.append(argv)
        if "status" in argv:
            if self.statuses:
                self._last = self.statuses.pop(0)
            return (0, self._last, "")
        if "connect" in argv:
            return (0, envelope("connecting"), "")
        return (0, envelope(), "")


def make_backend(runner):
    states = []
    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(),
        runner=runner,
        poll_interval=0.0,
        supervise_interval=0.01,
    )
    return backend, states


async def settle(times=40):
    for _ in range(times):
        await asyncio.sleep(0.01)


async def test_stream_dropping_marks_session_failed():
    runner = ScriptedRunner([
        envelope("streaming"),   # start() sees streaming
        envelope("idle"),        # supervisor sees it vanish
    ])
    backend, states = make_backend(runner)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.STREAMING

    await settle()
    assert states[-1][0] is SessionState.FAILED
    assert "stopped unexpectedly" in states[-1][1]
    await backend.shutdown()


async def test_supervisor_is_quiet_while_streaming_holds():
    runner = ScriptedRunner([envelope("streaming")])
    backend, states = make_backend(runner)
    await backend.start(make_device())
    before = len(states)

    await settle()
    assert len(states) == before
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


async def test_deliberate_stop_does_not_report_failure():
    runner = ScriptedRunner([envelope("streaming"), envelope("idle")])
    backend, states = make_backend(runner)
    device = make_device()
    await backend.start(device)
    await backend.stop(device)

    await settle()
    assert states[-1][0] is SessionState.IDLE
    assert not any(s is SessionState.FAILED for s, _ in states)


async def test_supervisor_stops_after_shutdown():
    runner = ScriptedRunner([envelope("streaming")])
    backend, _ = make_backend(runner)
    await backend.start(make_device())
    await backend.shutdown()
    calls = len(runner.calls)

    await settle()
    assert len(runner.calls) == calls


async def test_awaiting_pin_is_not_supervised_as_a_drop():
    """Waiting on a PIN can last minutes and must not be read as a crash."""
    runner = ScriptedRunner([
        envelope("pin_required", needs_pin=True),
        envelope("pin_required", needs_pin=True),
    ])
    backend, states = make_backend(runner)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.AWAITING_PIN

    await settle()
    assert not any(s is SessionState.FAILED for s, _ in states)
    await backend.shutdown()


async def test_binary_disappearing_is_reported_not_raised():
    """A supervisor exception must not escape into the event loop."""

    class Vanishing(ScriptedRunner):
        """Disappears only after the stream is up, so start() succeeds first."""

        def __init__(self, statuses):
            super().__init__(statuses)
            self.status_calls = 0

        async def __call__(self, argv):
            if "status" in argv:
                self.status_calls += 1
                if self.status_calls > 1:
                    raise FileNotFoundError("doubletake-ctl")
            return await super().__call__(argv)

    runner = Vanishing([envelope("streaming")])
    backend, states = make_backend(runner)
    await backend.start(make_device())

    await settle()
    assert states[-1][0] is SessionState.FAILED
    await backend.shutdown()
