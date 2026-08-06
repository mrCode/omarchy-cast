import asyncio

import pytest

from omarchy_cast.backends import airplay as airplay_mod
from omarchy_cast.backends.airplay import AirPlayBackend
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import EXTEND, MIRROR, SessionState

from tests.test_airplay_backend import READY, FakeProc


def make_device():
    return Device(
        id="airplay:AA", name="Living Room", address="192.168.1.77",
        port=7000, protocol="airplay",
    )


def make_device_b():
    return Device(
        id="airplay:BB", name="Bedroom", address="192.168.1.78",
        port=7000, protocol="airplay",
    )


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """No hyprctl, no display changes, no real credentials."""
    events = []
    monkeypatch.setattr(airplay_mod.virtual_display, "cleanup_strays",
                        lambda *a, **k: events.append("cleanup") or 0)
    monkeypatch.setattr(airplay_mod.virtual_display, "create",
                        lambda *a, **k: events.append("create") or "omarchy-cast")
    monkeypatch.setattr(airplay_mod.virtual_display, "remove",
                        lambda name, *a, **k: events.append(f"remove:{name}") or True)
    monkeypatch.setattr(airplay_mod.display, "apply_stream_mode",
                        lambda *a, **k: events.append("switch-display"))
    monkeypatch.setattr(airplay_mod.display, "restore_mode",
                        lambda *a, **k: events.append("restore-display") or True)
    monkeypatch.setattr(airplay_mod, "creds_path",
                        lambda mode: (tmp_path / "extend.json") if mode == EXTEND else None)
    return events


def make_backend(proc, **cfg):
    cfg.setdefault("airplay_auto_resolution", True)
    states = []
    spawned = {}

    async def spawner(argv, env):
        spawned["argv"] = argv
        return proc

    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(**cfg), spawner=spawner, ready_timeout=1.0,
    )
    return backend, states, spawned


async def test_extend_creates_a_virtual_output(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    assert "create" in fakes
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


async def test_extend_does_not_touch_the_display(fakes):
    """The virtual output is already 1080p; switching would be gratuitous."""
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    assert "switch-display" not in fakes
    await backend.shutdown()


async def test_mirror_still_switches_the_display(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), MIRROR)
    assert "switch-display" in fakes
    assert "create" not in fakes
    await backend.shutdown()


async def test_extend_passes_its_own_creds_file(fakes, tmp_path):
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    argv = spawned["argv"]
    assert "-creds" in argv
    assert argv[argv.index("-creds") + 1] == str(tmp_path / "extend.json")
    await backend.shutdown()


async def test_mirror_passes_no_creds_flag(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc)
    await backend.start(make_device(), MIRROR)
    assert "-creds" not in spawned["argv"]
    await backend.shutdown()


async def test_stop_removes_the_virtual_output(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    device = make_device()
    await backend.start(device, EXTEND)
    await backend.stop(device)
    assert "remove:omarchy-cast" in fakes


async def test_failed_start_removes_the_virtual_output(fakes):
    """A failure must not leave a phantom monitor on the desktop."""
    proc = FakeProc([b"mirror setup failed\n"], exit_on_eof=True)
    backend, _, _ = make_backend(proc)
    with pytest.raises(Exception):
        await backend.start(make_device(), EXTEND)
    assert any(e.startswith("remove:") for e in fakes)


async def test_shutdown_removes_the_virtual_output(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    await backend.shutdown()
    assert any(e.startswith("remove:") for e in fakes)


async def test_strays_are_cleaned_before_creating(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    assert fakes.index("cleanup") < fakes.index("create")
    await backend.shutdown()


async def test_extend_fails_clearly_when_the_output_cannot_be_created(
    fakes, monkeypatch
):
    monkeypatch.setattr(airplay_mod.virtual_display, "create", lambda *a, **k: None)
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    with pytest.raises(Exception, match="virtual display"):
        await backend.start(make_device(), EXTEND)
    assert states[-1][0] is SessionState.FAILED


# -- fix round 1: spawn failing before a session exists (Finding 1) ----


def make_raising_backend(exc, **cfg):
    """A backend whose spawner always raises, before any session is created."""
    cfg.setdefault("airplay_auto_resolution", True)
    states = []

    async def spawner(argv, env):
        raise exc

    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(**cfg), spawner=spawner, ready_timeout=1.0,
    )
    return backend, states


async def test_extend_removes_the_output_when_spawn_itself_raises(fakes):
    """The session never got far enough to be registered, so nothing else
    would ever clean this up -- the failure path itself must."""
    backend, states = make_raising_backend(RuntimeError("boom"))
    with pytest.raises(Exception):
        await backend.start(make_device(), EXTEND)
    assert "remove:omarchy-cast" in fakes
    assert states[-1][0] is SessionState.FAILED


async def test_mirror_restores_the_display_when_spawn_itself_raises(fakes):
    backend, states = make_raising_backend(RuntimeError("boom"))
    with pytest.raises(Exception):
        await backend.start(make_device(), MIRROR)
    assert "restore-display" in fakes
    assert states[-1][0] is SessionState.FAILED


# -- fix round 1: independent teardown for co-existing sessions (Finding 2) --


def make_two_device_backend(**cfg):
    cfg.setdefault("airplay_auto_resolution", True)
    procs = [FakeProc([READY + b"\n"]), FakeProc([READY + b"\n"])]

    async def spawner(argv, env):
        return procs.pop(0)

    backend = AirPlayBackend(
        lambda d, s, e: None, Config(**cfg), spawner=spawner, ready_timeout=1.0,
    )
    return backend


async def test_teardown_mirror_then_extend_restores_both(fakes):
    backend = make_two_device_backend()
    mirror_device, extend_device = make_device(), make_device_b()
    await backend.start(mirror_device, MIRROR)
    await backend.start(extend_device, EXTEND)

    # Starting already emits its own "clear stale state" restore-display call
    # (see test_start_switches_the_display_and_stop_restores_it); drop that
    # noise so the assertions below are about teardown, not startup.
    fakes.clear()
    await backend.stop(mirror_device)
    await backend.stop(extend_device)

    assert "restore-display" in fakes
    assert "remove:omarchy-cast" in fakes


async def test_teardown_extend_then_mirror_restores_both(fakes):
    backend = make_two_device_backend()
    mirror_device, extend_device = make_device(), make_device_b()
    await backend.start(mirror_device, MIRROR)
    await backend.start(extend_device, EXTEND)

    fakes.clear()
    await backend.stop(extend_device)
    await backend.stop(mirror_device)

    assert "restore-display" in fakes
    assert "remove:omarchy-cast" in fakes


# -- fix round 1: extend limited to one session at a time (Finding 3) --


async def test_second_extend_is_rejected_while_one_is_active(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    first, second = make_device(), make_device_b()
    await backend.start(first, EXTEND)

    with pytest.raises(Exception, match="already extending"):
        await backend.start(second, EXTEND)

    # Rejected before touching hyprctl again: the first output is untouched.
    assert "remove:omarchy-cast" not in fakes
    assert states[-1][0] is SessionState.FAILED

    # The first session is still alive and still cleans up normally.
    await backend.stop(first)
    assert "remove:omarchy-cast" in fakes


# -- fix round 2: the rejection must have no side effects (Finding 1) --


def make_recording_backend(count=2, **cfg):
    """A backend over `count` ready children, exposing the procs it handed out."""
    cfg.setdefault("airplay_auto_resolution", True)
    procs = [FakeProc([READY + b"\n"]) for _ in range(count)]
    handed = []
    states = []

    async def spawner(argv, env):
        proc = procs.pop(0)
        handed.append(proc)
        return proc

    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(**cfg), spawner=spawner, ready_timeout=1.0,
    )
    return backend, states, handed


async def test_a_rejected_extend_does_not_kill_the_requesting_device_mirror(fakes):
    """The guard used to run *after* start()'s unconditional teardown, so the
    request whose side effects had already fired was then refused: the user's
    live mirror on B was killed and the error only mentioned A."""
    backend, states, handed = make_recording_backend()
    extending, mirroring = make_device(), make_device_b()
    await backend.start(extending, EXTEND)
    await backend.start(mirroring, MIRROR)
    mirror_proc = handed[1]

    with pytest.raises(Exception, match="already extending"):
        await backend.start(mirroring, EXTEND)

    # B's mirror survived the refusal, process and session both.
    assert mirroring.id in backend._sessions
    assert backend._sessions[mirroring.id].proc is mirror_proc
    assert mirror_proc.terminated is False
    # ...and A's output was never touched either.
    assert "remove:omarchy-cast" not in fakes
    assert states[-1][0] is SessionState.FAILED

    await backend.shutdown()


async def test_re_extending_the_same_device_is_not_rejected(fakes):
    """Restarting the extend on the device that already owns it must still
    work -- the guard is about a *second* device stealing the output."""
    backend, states, _ = make_recording_backend()
    device = make_device()
    await backend.start(device, EXTEND)
    await backend.start(device, EXTEND)
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


# -- fix round 2: the guard must survive a race (Finding 4) --


async def test_two_extends_racing_produce_exactly_one_session(fakes):
    """The guard reads self._sessions, but a session is registered only after
    `await self._spawn(...)`, and create_subprocess_exec yields several times.
    A second extend landing in that window passed the guard too: both sessions
    claimed the one virtual output, only one output existed, and stopping
    either removed it from under the other -- which stayed registered and kept
    reporting STREAMING.
    """
    procs = [FakeProc([READY + b"\n"]), FakeProc([READY + b"\n"])]
    states = []

    async def spawner(argv, env):
        # A real spawn suspends here; the fake has to as well or the race the
        # guard has to survive cannot be expressed.
        await asyncio.sleep(0)
        return procs.pop(0)

    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(airplay_auto_resolution=True), spawner=spawner, ready_timeout=1.0,
    )

    results = await asyncio.gather(
        backend.start(make_device(), EXTEND),
        backend.start(make_device_b(), EXTEND),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(failures) == 1, f"expected exactly one refusal, got {results}"
    assert "already extending" in str(failures[0])
    assert len(backend._sessions) == 1
    # One winner means one virtual output was ever created.
    assert fakes.count("create") == 1

    await backend.shutdown()


async def test_mirror_start_while_extend_is_active_leaves_the_output_alone(fakes):
    backend = make_two_device_backend()
    extend_device, mirror_device = make_device(), make_device_b()

    await backend.start(extend_device, EXTEND)
    await backend.start(mirror_device, MIRROR)

    assert "remove:omarchy-cast" not in fakes
    assert "switch-display" in fakes

    await backend.shutdown()
    assert "remove:omarchy-cast" in fakes
