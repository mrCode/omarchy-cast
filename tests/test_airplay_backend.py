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
from omarchy_cast.core.session import SessionState

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


def make_backend(proc=None, ready_timeout=1.0, **cfg):
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
        lambda d, s, e: states.append((s, e)), Config(), spawner=spawner
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
        lambda d, s, e: states.append((s, e)), Config(), spawner=spawner,
        ready_timeout=1.0,
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
