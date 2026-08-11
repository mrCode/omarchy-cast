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
    # Return whichever name was requested: mirror and extend use different
    # outputs, and a stub that always says "omarchy-cast" would hide a mirror
    # session tearing down extend's output.
    def _create(*a, want=None, mirror_of=None, **k):
        name = want or airplay_mod.virtual_display.VIRTUAL_NAME
        events.append(f"create:{name}" + (f":mirror-of={mirror_of}" if mirror_of else ""))
        return name

    monkeypatch.setattr(airplay_mod.virtual_display, "create", _create)
    monkeypatch.setattr(airplay_mod.virtual_display, "focused_monitor",
                        lambda *a, **k: "eDP-2")
    monkeypatch.setattr(airplay_mod.virtual_display, "remove",
                        lambda name, *a, **k: events.append(f"remove:{name}") or True)
    monkeypatch.setattr(airplay_mod.display, "apply_stream_mode",
                        lambda *a, **k: events.append("switch-display"))
    monkeypatch.setattr(airplay_mod.display, "restore_mode",
                        lambda *a, **k: events.append("restore-display") or True)
    def _creds(mode, virtual=False):
        if mode == EXTEND:
            return tmp_path / "extend.json"
        return (tmp_path / "mirror.json") if virtual else None

    monkeypatch.setattr(airplay_mod, "creds_path", _creds)
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
    assert "create:omarchy-cast" in fakes
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


async def test_extend_does_not_touch_the_display(fakes):
    """The virtual output is already 1080p; switching would be gratuitous."""
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    assert "switch-display" not in fakes
    await backend.shutdown()


async def test_mirror_uses_a_mirrored_output_and_leaves_the_panel_alone(fakes):
    """Mirror used to force the PANEL to 1920x1080. On a laptop offering only
    2560x1600 at 240Hz or 60Hz, Hyprland synthesised 1080p at 60Hz -- so the
    user typed on a display four times slower and blamed the cast. A virtual
    output mirroring the panel gives the same 1080p source for free."""
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)

    await backend.start(make_device(), MIRROR)

    assert "create:omarchy-cast-mirror:mirror-of=eDP-2" in fakes
    assert "switch-display" not in fakes
    await backend.shutdown()


async def test_mirror_falls_back_to_switching_when_no_output_can_be_made(
    fakes, monkeypatch
):
    """A slower panel beats refusing to cast at all."""
    monkeypatch.setattr(
        airplay_mod.virtual_display, "create", lambda *a, **k: None
    )
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)

    await backend.start(make_device(), MIRROR)

    assert "switch-display" in fakes
    await backend.shutdown()


async def test_mirror_and_extend_use_different_outputs(fakes):
    """Sharing one name meant either session's cleanup destroyed the other's
    live output -- the failure shape this project has already shipped once."""
    backend = make_two_device_backend()
    await backend.start(make_device(), MIRROR)
    await backend.start(make_device_b(), EXTEND)

    virtuals = sorted(s.virtual for s in backend._sessions.values())

    assert virtuals == ["omarchy-cast", "omarchy-cast-mirror"]
    await backend.shutdown()


async def test_extend_passes_its_own_creds_file(fakes, tmp_path):
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    argv = spawned["argv"]
    assert "-creds" in argv
    assert argv[argv.index("-creds") + 1] == str(tmp_path / "extend.json")
    await backend.shutdown()


async def test_mirror_passes_its_own_creds_when_it_captures_a_virtual_output(
    fakes, tmp_path
):
    """Mirror now captures a virtual output, so doubletake's default token --
    which points at the real panel -- would silently select the panel instead."""
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc)

    await backend.start(make_device(), MIRROR)

    assert "-creds" in spawned["argv"]
    assert str(tmp_path / "mirror.json") in spawned["argv"]
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
    assert fakes.index("cleanup") < fakes.index("create:omarchy-cast")
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

    # Mirror removes its mirrored output; extend removes its own. Neither
    # touches the panel any more.
    assert "remove:omarchy-cast-mirror" in fakes
    assert "remove:omarchy-cast" in fakes


async def test_teardown_extend_then_mirror_restores_both(fakes):
    backend = make_two_device_backend()
    mirror_device, extend_device = make_device(), make_device_b()
    await backend.start(mirror_device, MIRROR)
    await backend.start(extend_device, EXTEND)

    fakes.clear()
    await backend.stop(extend_device)
    await backend.stop(mirror_device)

    # Mirror removes its mirrored output; extend removes its own. Neither
    # touches the panel any more.
    assert "remove:omarchy-cast-mirror" in fakes
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


# -- fix round 2: the crash diagnosis has to reach the log (Finding 9) --


async def _wait_for_failed(states, proc):
    proc.die()
    for _ in range(80):
        await asyncio.sleep(0.01)
        if states and states[-1][0] is SessionState.FAILED:
            return
    raise AssertionError(f"no FAILED emit; got {states}")


async def test_a_crash_is_written_to_the_log_not_just_notified(fakes, caplog):
    """The full diagnostic went only to a transient desktop notification, so
    daemon.log could not explain why a session ended -- a gap hit during this
    branch's own hardware testing."""
    import logging

    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    with caplog.at_level(logging.WARNING, logger="omarchy_cast.backends.airplay"):
        await backend.start(make_device(), MIRROR)
        await _wait_for_failed(states, proc)
    assert "stopped unexpectedly" in caplog.text
    assert "Living Room" in caplog.text


async def test_a_crashed_extend_does_not_call_itself_a_mirror(fakes, caplog):
    """The message hardcoded "mirroring to X stopped unexpectedly" for every
    session, so an extend that died reported a mirror that never existed."""
    import logging

    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    with caplog.at_level(logging.WARNING, logger="omarchy_cast.backends.airplay"):
        await backend.start(make_device(), EXTEND)
        await _wait_for_failed(states, proc)
    assert "extending to Living Room stopped unexpectedly" in states[-1][1]
    assert "mirroring" not in states[-1][1]
    assert "extending to Living Room stopped unexpectedly" in caplog.text


async def test_a_crashed_mirror_still_says_mirroring(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device(), MIRROR)
    await _wait_for_failed(states, proc)
    assert "mirroring to Living Room stopped unexpectedly" in states[-1][1]


# -- fix round 2: a removal that failed is not a successful stop (Finding 6) --


async def test_stop_reports_a_virtual_output_it_could_not_remove(fakes, monkeypatch):
    """virtual_display.remove() returns a bool and nothing looked at it, so
    stop answered {"ok": true, "stopped": 1} and the menu notified "Stopped
    casting" while a phantom 1080p monitor stayed on the desktop."""
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    device = make_device()
    await backend.start(device, EXTEND)

    monkeypatch.setattr(
        airplay_mod.virtual_display, "remove", lambda name, *a, **k: False
    )
    with pytest.raises(Exception, match="omarchy-cast"):
        await backend.stop(device)

    # The session really is gone; the error is about what was left behind.
    assert states[-1][0] is SessionState.IDLE
    assert device.id not in backend._sessions


async def test_stop_stays_quiet_when_the_output_really_went_away(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    device = make_device()
    await backend.start(device, EXTEND)
    await backend.stop(device)
    assert states[-1][0] is SessionState.IDLE


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
    assert fakes.count("create:omarchy-cast") == 1

    await backend.shutdown()


async def test_mirror_start_while_extend_is_active_leaves_the_output_alone(fakes):
    backend = make_two_device_backend()
    extend_device, mirror_device = make_device(), make_device_b()

    await backend.start(extend_device, EXTEND)
    await backend.start(mirror_device, MIRROR)

    # Mirror makes its OWN output and must not disturb extend's.
    assert "remove:omarchy-cast" not in fakes
    assert "create:omarchy-cast-mirror:mirror-of=eDP-2" in fakes

    await backend.shutdown()
    assert "remove:omarchy-cast" in fakes


async def test_a_failed_extend_restart_does_not_resurrect_the_dead_session(
    fakes, monkeypatch
):
    """Guards the classification of the virtual-display failure, not its message.

    It is the one failure in start() that READS like a refusal -- "could not
    create a virtual display" sounds like the backend declined. It is not: it
    happens after _teardown has already killed the child and removed the
    output, so nothing survives it. Raising BackendRefused there would tell the
    daemon to restore the session record it displaced, putting a green
    "streaming" indicator on the bar for an extend that is gone.

    The whole suite stays green under that one-word change, which is why this
    test exists: it asserts through the daemon, where the consequence lives.
    """
    from omarchy_cast.core.daemon import Daemon

    class FakeDiscovery:
        def __init__(self, devices):
            self._devices = list(devices)

        def devices(self):
            return self._devices

        def add(self, device):
            self._devices.append(device)

        def start(self):
            pass

        def stop(self):
            pass

    device = make_device()
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    daemon = Daemon(FakeDiscovery([device]), {}, notifier=lambda m: None)
    backend._on_state = daemon.on_state
    daemon.backends["airplay"] = backend

    await daemon.handle({"cmd": "start", "device_id": device.id, "mode": "extend"})
    assert daemon.sessions[device.id].state is SessionState.STREAMING

    # The user restarts the extend; this time the output cannot be recreated.
    monkeypatch.setattr(airplay_mod.virtual_display, "create", lambda *a, **k: None)
    resp = await daemon.handle(
        {"cmd": "start", "device_id": device.id, "mode": "extend"}
    )

    assert resp["ok"] is False
    assert "could not create a virtual display" in resp["error"]
    assert daemon.sessions == {}
    assert (await daemon.handle({"cmd": "status"}))["data"]["sessions"] == []
    await backend.shutdown()
