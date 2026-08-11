"""AirPlay backend: supervises a direct `doubletake -target` child per session.

Daemon mode is deliberately not used. doubletake 0.4.0's daemon.Config has no
PortMin/PortMax, so -port-range is silently dropped there and the receiver's
reverse handshake lands on ephemeral ports a firewall discards. Direct mode
honours the flag and is the mode confirmed working on real hardware.
"""

import asyncio

import pytest

from omarchy_cast.backends.airplay import (
    PIN_PROMPT,
    READY_MARKERS,
    AirPlayBackend,
)
from omarchy_cast.backends.base import BackendError
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import EXTEND, SessionState

READY = READY_MARKERS[0].encode()


def make_device():
    return Device(
        id="airplay:AA", name="Living Room", address="192.168.1.77",
        port=7000, protocol="airplay",
    )


class FakeProc:
    """Mimics a doubletake child: yields output chunks, then blocks or exits."""

    def __init__(self, chunks=(), exit_on_eof=False, exit_code=1):
        self._chunks = list(chunks)
        self._exit_on_eof = exit_on_eof
        self._exit_code = exit_code
        self._dead = False
        self.stdin = []
        self.terminated = False
        self.returncode = None

    async def read(self, n=4096):
        # Re-checks the queue each tick so chunks fed later are still delivered.
        while True:
            if self._chunks:
                return self._chunks.pop(0)
            if self._exit_on_eof or self._dead or self.terminated:
                if self.returncode is None:
                    self.returncode = self._exit_code
                return b""
            await asyncio.sleep(0.005)

    def die(self):
        """Simulate the child crashing."""
        self._dead = True

    async def write(self, data):
        self.stdin.append(data)

    def feed(self, chunk):
        self._chunks.append(chunk)

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.005)
        return self.returncode


def make_backend(proc=None, ready_timeout=1.0, route_check=None, **cfg):
    # Never touch the real display from tests. Switching is covered by
    # test_display.py and by the integration tests below, both with fakes.
    cfg.setdefault("airplay_auto_resolution", False)
    states = []
    spawned = {}

    async def spawner(argv, env):
        spawned["argv"] = argv
        spawned["env"] = env
        return proc if proc is not None else FakeProc()

    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(**cfg),
        spawner=spawner,
        ready_timeout=ready_timeout,
        # Same subnet unless a test says otherwise: the real check would read
        # this machine's routing table and make the message environment-dependent.
        route_check=route_check or (lambda addr: False),
    )
    return backend, states, spawned


# -- argv construction -------------------------------------------------


def test_argv_targets_the_device_and_pins_ports():
    backend, _, _ = make_backend(airplay_port_range="60000-60010")
    argv = backend.build_argv(make_device())
    assert argv[0] == "doubletake"
    assert "-target" in argv and "192.168.1.77" in argv
    assert "-port-range" in argv and "60000-60010" in argv


def test_argv_never_uses_daemon_mode():
    """Daemon mode drops -port-range; that is the whole reason for this design."""
    backend, _, _ = make_backend()
    assert "-daemonize" not in backend.build_argv(make_device())


def test_argv_maps_encoder_choice_to_hwaccel():
    """Otherwise doubletake auto-picks NVENC, contradicting our iGPU-first rule."""
    for encoder, expected in (("vaapi", "vaapi"), ("nvenc", "nvenc"), ("x264", "none")):
        backend, _, _ = make_backend(encoder=encoder)
        argv = backend.build_argv(make_device())
        assert argv[argv.index("-hwaccel") + 1] == expected


def test_argv_omits_bitrate_when_auto():
    backend, _, _ = make_backend(airplay_bitrate=0)
    assert "-bitrate" not in backend.build_argv(make_device())


def test_argv_includes_bitrate_when_set():
    backend, _, _ = make_backend(airplay_bitrate=4500)
    argv = backend.build_argv(make_device())
    assert argv[argv.index("-bitrate") + 1] == "4500"


def test_argv_carries_fps():
    backend, _, _ = make_backend(fps=60)
    argv = backend.build_argv(make_device())
    assert argv[argv.index("-fps") + 1] == "60"


# -- lifecycle ---------------------------------------------------------


async def test_start_reaches_streaming_on_ready_marker():
    proc = FakeProc([b"connected to: Living Room\n", READY + b"\n"])
    backend, states, spawned = make_backend(proc)
    await backend.start(make_device())
    assert states[0][0] is SessionState.CONNECTING
    assert states[-1][0] is SessionState.STREAMING
    assert spawned["argv"][0] == "doubletake"
    await backend.shutdown()


async def test_ready_marker_split_across_chunks_is_still_detected():
    """Output arrives in arbitrary chunks, not lines."""
    half = len(READY) // 2
    proc = FakeProc([READY[:half], READY[half:] + b"\n"])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


async def test_pin_prompt_enters_awaiting_pin():
    """The prompt has no trailing newline, so line reads would never see it."""
    proc = FakeProc([PIN_PROMPT.encode()])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.AWAITING_PIN
    await backend.shutdown()


async def test_submit_pin_writes_to_stdin_and_streams():
    proc = FakeProc([PIN_PROMPT.encode()])
    backend, states, _ = make_backend(proc)
    device = make_device()
    await backend.start(device)
    proc.feed(READY + b"\n")
    await backend.submit_pin(device, "4029")
    assert proc.stdin == [b"4029\n"]
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


async def test_submit_pin_without_a_session_is_actionable():
    backend, _, _ = make_backend()
    with pytest.raises(BackendError, match="no pending"):
        await backend.submit_pin(make_device(), "4029")


async def test_process_exiting_before_ready_fails_with_its_output():
    proc = FakeProc([b"mirror setup failed: something broke\n"], exit_on_eof=True)
    backend, states, _ = make_backend(proc)
    with pytest.raises(BackendError, match="something broke"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED


async def test_timeout_before_ready_blames_the_reverse_handshake():
    proc = FakeProc([b"connected to: Living Room\n"])
    backend, states, _ = make_backend(proc, ready_timeout=0.05)
    with pytest.raises(BackendError, match="firewall"):
        await backend.start(make_device())
    assert proc.terminated is True
    assert states[-1][0] is SessionState.FAILED


async def test_timeout_message_mentions_the_configured_range():
    proc = FakeProc([])
    backend, _, _ = make_backend(proc, ready_timeout=0.05, airplay_port_range="60000-60010")
    with pytest.raises(BackendError, match="60000-60010"):
        await backend.start(make_device())


async def test_stop_terminates_the_child():
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert proc.terminated is True
    assert states[-2:] == [(SessionState.STOPPING, None), (SessionState.IDLE, None)]


async def test_child_dying_mid_stream_is_reported():
    """Supervision is now just process exit -- no polling required."""
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.STREAMING

    proc.die()
    for _ in range(80):
        await asyncio.sleep(0.01)
        if states[-1][0] is SessionState.FAILED:
            break
    assert states[-1][0] is SessionState.FAILED
    assert "stopped unexpectedly" in states[-1][1]


async def test_missing_binary_is_actionable():
    async def spawner(argv, env):
        raise FileNotFoundError(argv[0])

    states = []
    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(airplay_auto_resolution=False), spawner=spawner,
    )
    with pytest.raises(BackendError, match="doubletake"):
        await backend.start(make_device())


async def test_configured_code_is_passed_in_the_environment():
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc, airplay_code="1234")
    await backend.start(make_device())
    assert spawned["env"].get("DOUBLETAKE_CODE") == "1234"
    await backend.shutdown()


async def test_no_code_means_no_env_var():
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc)
    await backend.start(make_device())
    assert "DOUBLETAKE_CODE" not in spawned["env"]
    await backend.shutdown()


async def test_shutdown_terminates_every_session():
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device())
    await backend.shutdown()
    assert proc.terminated is True


async def test_second_start_replaces_the_first_child():
    procs = [FakeProc([READY + b"\n"]), FakeProc([READY + b"\n"])]
    states = []

    async def spawner(argv, env):
        return procs.pop(0)

    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(airplay_auto_resolution=False),
        spawner=spawner, ready_timeout=1.0,
    )
    device = make_device()
    first = procs[0]
    await backend.start(device)
    await backend.start(device)
    assert first.terminated is True
    await backend.shutdown()


async def test_deliberate_stop_never_reports_a_crash():
    """Ported from the old daemon-mode supervision suite: stopping is not dying."""
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert not any(s is SessionState.FAILED for s, _ in states)


async def test_dying_while_awaiting_pin_is_not_reported_as_a_crash():
    """A PIN wait can last minutes; only a live stream dying is a crash."""
    proc = FakeProc([PIN_PROMPT.encode()])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.AWAITING_PIN

    proc.die()
    for _ in range(30):
        await asyncio.sleep(0.01)
    assert not any(
        e and "stopped unexpectedly" in e for _, e in states if e
    )
    await backend.shutdown()


async def test_pump_survives_a_read_error():
    """An exception in the reader must not escape into the event loop."""

    class Exploding(FakeProc):
        async def read(self, n=4096):
            if self._chunks:
                return self._chunks.pop(0)
            raise OSError("pipe went away")

    proc = Exploding([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    # The child dies during startup, so start() reports it rather than
    # announcing STREAMING for a process that is already gone.
    with pytest.raises(BackendError):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED


async def test_mirror_session_ready_alone_is_not_streaming():
    """Verified against real hardware: 'mirror session ready' fires ~4s before
    'screen capture started', before the capture pipeline runs. Treating it as
    ready reports STREAMING for a stream with no pixels, and would mask a
    capture failure as success.
    """
    proc = FakeProc([
        b"connected to: Living Room\n",
        b"FairPlay setup complete\n",
        b"mirror session ready (data port: 49277)\n",
    ])
    backend, states, _ = make_backend(proc, ready_timeout=0.15)
    with pytest.raises(BackendError):
        await backend.start(make_device())
    assert not any(s is SessionState.STREAMING for s, _ in states)


async def test_capture_started_after_session_ready_is_streaming():
    proc = FakeProc([
        b"mirror session ready (data port: 49277)\n",
        b"screen capture started\n",
    ])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


# -- vapostproc shim ---------------------------------------------------


def test_shim_hides_vapostproc_but_passes_everything_else(tmp_path, monkeypatch):
    """doubletake probes with `gst-inspect-1.0 vapostproc` and uses the element
    if present. On Hyprland it is present but cannot import the portal's padded
    DMA-BUF, so the pipeline emits nothing and the receiver shows black with no
    error. Reporting it absent forces the working videoconvert path.
    """
    import subprocess

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    backend, _, _ = make_backend()
    shim = backend.shim_dir() / "gst-inspect-1.0"

    assert shim.exists()
    assert shim.stat().st_mode & 0o111, "shim must be executable"
    assert subprocess.run([str(shim), "vapostproc"]).returncode != 0
    # Anything else must pass through to the real binary.
    assert "exec " in shim.read_text()


def test_env_prepends_the_shim_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    backend, _, _ = make_backend(airplay_hide_vapostproc=True)
    assert backend.daemon_env()["PATH"].startswith(str(backend.shim_dir()))


def test_env_leaves_path_alone_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    backend, _, _ = make_backend(airplay_hide_vapostproc=False)
    assert backend.daemon_env()["PATH"] == "/usr/bin"


def test_shim_is_regenerated_idempotently(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    backend, _, _ = make_backend()
    first = backend.shim_dir()
    assert backend.shim_dir() == first


# -- display switching integration -------------------------------------


async def test_start_switches_the_display_and_stop_restores_it(monkeypatch):
    """The receiver rejects a stream whose SPS does not match 1920x1080."""
    from omarchy_cast.core import display as display_mod

    calls = []
    monkeypatch.setattr(display_mod, "apply_stream_mode", lambda *a, **k: calls.append("apply"))
    monkeypatch.setattr(display_mod, "restore_mode", lambda *a, **k: calls.append("restore") or True)

    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc, airplay_auto_resolution=True)
    device = make_device()
    await backend.start(device)
    # start() clears any mode left over from a crash first, so a restore may
    # precede the apply; what matters is that apply is the last thing done.
    assert calls[-1] == "apply"
    await backend.stop(device)
    assert calls[-1] == "restore"


async def test_display_is_restored_when_start_fails(monkeypatch):
    """A failed start must not strand the user at 1080p."""
    from omarchy_cast.core import display as display_mod

    calls = []
    monkeypatch.setattr(display_mod, "apply_stream_mode", lambda *a, **k: calls.append("apply"))
    monkeypatch.setattr(display_mod, "restore_mode", lambda *a, **k: calls.append("restore") or True)

    proc = FakeProc([b"mirror setup failed\n"], exit_on_eof=True)
    backend, _, _ = make_backend(proc, airplay_auto_resolution=True)
    with pytest.raises(BackendError):
        await backend.start(make_device())
    assert "restore" in calls


async def test_display_untouched_when_disabled(monkeypatch):
    from omarchy_cast.core import display as display_mod

    calls = []
    monkeypatch.setattr(display_mod, "apply_stream_mode", lambda *a, **k: calls.append("apply"))
    monkeypatch.setattr(display_mod, "restore_mode", lambda *a, **k: calls.append("restore") or True)

    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc, airplay_auto_resolution=False)
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert calls == []


# -- the stall message must not assert a cause it did not check --------------


async def test_a_receiver_on_another_subnet_is_named_as_the_cause():
    """This message used to blame the firewall outright. On a real network the
    laptop was on 172.26.x, the Apple TV on 10.10.10.x, the firewall logged not
    one drop, and the actual problem was routing. Telling the user to add a
    firewall rule there sends them somewhere no rule can help."""
    backend, _, _ = make_backend(
        FakeProc([]), ready_timeout=0.05, route_check=lambda addr: True
    )

    with pytest.raises(BackendError) as exc:
        await backend.start(make_device())

    message = str(exc.value)
    assert "different subnet" in message
    # Cross-subnet is not fatal: a cast to an Apple TV two subnets away streamed
    # fine once the firewall allowed that source. The message must not tell the
    # user to abandon a setup that works, and must still give the rule.
    assert "ufw allow" in message
    assert "cannot get back here at all" not in message


async def test_a_receiver_on_this_subnet_still_points_at_the_firewall():
    backend, _, _ = make_backend(
        FakeProc([]), ready_timeout=0.05, route_check=lambda addr: False,
        airplay_port_range="60000-60010",
    )

    with pytest.raises(BackendError) as exc:
        await backend.start(make_device())

    message = str(exc.value)
    assert "ufw allow" in message
    assert "60000:60010" in message
    assert "different subnet" not in message


async def test_an_unknown_route_does_not_claim_a_subnet_problem():
    """routed_via_gateway returns None when it cannot tell. A guess presented
    as a fact is what made the old message harmful."""
    backend, _, _ = make_backend(
        FakeProc([]), ready_timeout=0.05, route_check=lambda addr: None
    )

    with pytest.raises(BackendError) as exc:
        await backend.start(make_device())

    assert "different subnet" not in str(exc.value)


async def test_a_portal_failure_is_reported_as_itself_not_as_a_firewall():
    """doubletake says exactly what happened; we were discarding it and
    guessing. Extend strips its restore token so the user can pick the
    omarchy-cast output -- if nobody answers that dialog this is the result,
    and pointing at the firewall sent the user somewhere no rule can help."""
    proc = FakeProc([
        b"mirror session ready (data port: 49217)\n",
        b"screen capture failed: screencast portal: session response: "
        b"timeout waiting for portal response\n",
    ])
    backend, _, _ = make_backend(proc, ready_timeout=0.2)

    with pytest.raises(BackendError) as exc:
        await backend.start(make_device(), EXTEND)

    message = str(exc.value)
    assert "screen-share prompt" in message
    assert "omarchy-cast" in message
    assert "ufw" not in message
    assert "firewall" not in message.lower()
