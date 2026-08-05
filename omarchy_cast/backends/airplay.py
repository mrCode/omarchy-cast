"""AirPlay mirroring by supervising a direct `doubletake -target` child.

Why not daemon mode
-------------------
doubletake 0.4.0's `daemon.Config` (internal/daemon/daemon.go) carries no
PortMin/PortMax. `-port-range` is parsed in cmd/doubletake/main.go and threaded
into StreamConfig on the direct path only, so under `-daemonize` it is silently
dropped and the receiver's reverse handshake lands on random ephemeral ports.
With a default-DROP firewall those are discarded and SETUP stalls or returns
HTTP 401.

Measured on the same AppleTV11,1 with identical flags:

    -daemonize      UDP 36760-36762, TCP 45771   -> stalls, fails
    -target         UDP 60000-60002, TCP 60003   -> mirrors successfully

So each session is its own child process. That also makes crash detection a
plain process exit rather than a polling loop.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from collections.abc import Awaitable, Callable

from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState

log = logging.getLogger(__name__)

BIN = "doubletake"

# doubletake prints this with fmt.Print -- no trailing newline -- so output must
# be scanned as chunks, not lines.
PIN_PROMPT = "Enter the PIN shown on Apple TV"

# Only this marker means pixels are actually flowing. "mirror session ready"
# is emitted ~4s earlier, as soon as the RTSP session is set up and before the
# capture pipeline starts (and before the portal has necessarily been
# answered), so treating it as ready reports STREAMING for a stream that does
# not yet exist -- and would mask a capture failure as success.
READY_MARKERS = ("screen capture started",)

# Progress markers: useful for diagnostics, never taken as ready.
PROGRESS_MARKERS = ("mirror session ready", "FairPlay setup complete")

# Our encoder vocabulary -> doubletake's -hwaccel vocabulary.
HWACCEL_MAP = {"auto": "auto", "vaapi": "vaapi", "nvenc": "nvenc", "x264": "none"}

READY_TIMEOUT = 30.0
MAX_BUFFER = 16384

Spawner = Callable[[list[str], dict], Awaitable["ProcessLike"]]


class ProcessLike:
    """Minimal surface the backend needs from a child process."""

    async def read(self, n: int = 4096) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    def terminate(self) -> None: ...
    async def wait(self) -> int: ...


class _Process:
    """Wraps an asyncio subprocess with stderr folded into stdout."""

    def __init__(self, proc) -> None:
        self._proc = proc

    @property
    def returncode(self):
        return self._proc.returncode

    async def read(self, n: int = 4096) -> bytes:
        return await self._proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    def terminate(self) -> None:
        if self._proc.returncode is None:
            self._proc.terminate()

    async def wait(self) -> int:
        return await self._proc.wait()


async def subprocess_spawner(argv: list[str], env: dict) -> _Process:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    return _Process(proc)


class _Session:
    def __init__(self, device: Device, proc) -> None:
        self.device = device
        self.proc = proc
        self.output = ""
        self.ready = asyncio.Event()
        self.needs_pin = asyncio.Event()
        self.exited = asyncio.Event()
        self.stopping = False
        # Set once STREAMING has actually been reported. The pump only treats a
        # process exit as a crash after that; before it, _await_ready owns the
        # outcome, so a startup failure cannot be overwritten by a late
        # STREAMING emit.
        self.streaming = False
        self.pump: asyncio.Task | None = None

    def absorb(self, chunk: bytes) -> None:
        self.output += chunk.decode("utf-8", "replace")
        if len(self.output) > MAX_BUFFER:
            self.output = self.output[-MAX_BUFFER:]
        if not self.ready.is_set() and any(m in self.output for m in READY_MARKERS):
            self.ready.set()
        if not self.needs_pin.is_set() and PIN_PROMPT in self.output:
            self.needs_pin.set()

    def tail(self, limit: int = 300) -> str:
        return " ".join(self.output.split())[-limit:]


class AirPlayBackend(Backend):
    protocol = "airplay"

    def __init__(
        self,
        on_state: StateCallback,
        config: Config,
        spawner: Spawner | None = None,
        ready_timeout: float = READY_TIMEOUT,
    ) -> None:
        super().__init__(on_state)
        self._config = config
        self._spawn = spawner or subprocess_spawner
        self._ready_timeout = ready_timeout
        self._sessions: dict[str, _Session] = {}

    # -- process construction --------------------------------------------

    def build_argv(self, device: Device) -> list[str]:
        argv = [
            BIN,
            "-target", device.address,
            "-port-range", self._config.airplay_port_range,
            "-fps", str(self._config.fps),
            "-hwaccel", HWACCEL_MAP.get(self._config.encoder, "auto"),
        ]
        if self._config.airplay_bitrate:
            argv += ["-bitrate", str(self._config.airplay_bitrate)]
        return argv

    def shim_dir(self) -> Path:
        """Create a PATH shim that hides vapostproc from doubletake.

        doubletake probes with `gst-inspect-1.0 vapostproc` and uses the element
        when it exists. On Hyprland the element exists but cannot import the
        portal's padded DMA-BUF, so the pipeline emits nothing and the receiver
        shows a black screen with no error anywhere. Reporting the element as
        absent makes doubletake fall back to videoconvert, which works.

        Generated at runtime rather than shipped, so packaging never has to
        preserve an executable bit.
        """
        base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        path = Path(base) / "omarchy-cast-shims"
        path.mkdir(parents=True, exist_ok=True)

        real = shutil.which("gst-inspect-1.0", path="/usr/bin:/usr/local/bin") or "gst-inspect-1.0"
        script = path / "gst-inspect-1.0"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "# Generated by omarchy-cast. Reports vapostproc as unavailable so\n"
            "# doubletake falls back to videoconvert; see AirPlayBackend.shim_dir.\n"
            f'[ "$1" = "vapostproc" ] && exit 1\n'
            f'exec {real} "$@"\n'
        )
        script.chmod(0o755)
        return path

    def daemon_env(self) -> dict[str, str]:
        """Environment for the doubletake child."""
        env = dict(os.environ)

        # DOUBLETAKE_CODE is plumbed ahead of upstream #26 landing.
        if self._config.airplay_code:
            env["DOUBLETAKE_CODE"] = self._config.airplay_code
        else:
            env.pop("DOUBLETAKE_CODE", None)

        if self._config.airplay_hide_vapostproc:
            env["PATH"] = f"{self.shim_dir()}:{env.get('PATH', '')}"

        return env

    # -- lifecycle ---------------------------------------------------------

    async def start(self, device: Device) -> None:
        await self._teardown(device.id)
        self._emit(device, SessionState.CONNECTING)

        try:
            proc = await self._spawn(self.build_argv(device), self.daemon_env())
        except FileNotFoundError as exc:
            message = f"{BIN} is not installed; install it with: yay -S doubletake"
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message) from exc

        session = _Session(device, proc)
        self._sessions[device.id] = session
        session.pump = asyncio.create_task(self._pump(session))

        await self._await_ready(session, initial=True)

    async def _await_ready(self, session: _Session, initial: bool) -> None:
        device = session.device
        waiters = [
            asyncio.ensure_future(session.ready.wait()),
            asyncio.ensure_future(session.exited.wait()),
        ]
        if initial:
            waiters.append(asyncio.ensure_future(session.needs_pin.wait()))

        try:
            done, pending = await asyncio.wait(
                waiters, timeout=self._ready_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for w in waiters:
                w.cancel()

        # Exit is checked first: a child that reached the ready marker and then
        # died has still failed, and reporting STREAMING would be a lie.
        if session.exited.is_set():
            message = (
                f"{BIN} exited before mirroring started: {session.tail() or 'no output'}"
            )
            await self._fail(session, message)
            raise BackendError(message)

        if session.ready.is_set():
            session.streaming = True
            self._emit(device, SessionState.STREAMING)
            return

        if initial and session.needs_pin.is_set():
            self._emit(device, SessionState.AWAITING_PIN)
            return

        message = (
            f"{device.name} never started mirroring within "
            f"{self._ready_timeout:.0f}s. The receiver connects back to this "
            f"machine on {self._config.airplay_port_range}; a default-DROP "
            f"firewall silently drops that and SETUP stalls. Allow inbound TCP "
            f"and UDP on that range from {device.address}."
        )
        await self._fail(session, message)
        raise BackendError(message)

    async def submit_pin(self, device: Device, pin: str) -> None:
        session = self._sessions.get(device.id)
        if session is None:
            raise BackendError(f"no pending session for {device.name}")
        await session.proc.write(f"{pin}\n".encode())
        await self._await_ready(session, initial=False)

    async def stop(self, device: Device) -> None:
        self._emit(device, SessionState.STOPPING)
        await self._teardown(device.id)
        self._emit(device, SessionState.IDLE)

    async def shutdown(self) -> None:
        for device_id in list(self._sessions):
            await self._teardown(device_id)

    # -- internals ---------------------------------------------------------

    async def _pump(self, session: _Session) -> None:
        """Read the child's merged output until it exits."""
        try:
            while True:
                chunk = await session.proc.read()
                if not chunk:
                    break
                session.absorb(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("output pump failed", exc_info=True)
        finally:
            session.exited.set()
            # Only a crash after STREAMING was reported; before that,
            # _await_ready owns the outcome.
            if session.streaming and not session.stopping:
                self._sessions.pop(session.device.id, None)
                self._emit(
                    session.device,
                    SessionState.FAILED,
                    f"mirroring to {session.device.name} stopped unexpectedly "
                    f"({session.tail(120) or 'process exited'})",
                )

    async def _fail(self, session: _Session, message: str) -> None:
        await self._teardown(session.device.id)
        self._emit(session.device, SessionState.FAILED, message)

    async def _teardown(self, device_id: str) -> None:
        session = self._sessions.pop(device_id, None)
        if session is None:
            return
        session.stopping = True
        session.proc.terminate()
        if session.pump is not None:
            session.pump.cancel()
            try:
                await session.pump
            except (asyncio.CancelledError, Exception):
                pass
