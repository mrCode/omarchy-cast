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
import contextlib
import logging
import os
import signal
import shutil
from pathlib import Path
from collections.abc import Awaitable, Callable

from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.backends.creds import creds_path
from omarchy_cast.core import display, virtual_display
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import EXTEND, MIRROR, SessionState

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
    """Wraps an asyncio subprocess with stderr folded into stdout.

    The child runs in its own process group so terminate() can take its
    GStreamer capture pipelines with it. Signalling only doubletake leaves
    those running, re-parented to init, still holding a portal node and the
    GPU -- five of them accumulated during one bad TUI session.
    """

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
        if self._proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            # Fall back to signalling just the child.
            with contextlib.suppress(ProcessLookupError):
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
        start_new_session=True,   # own process group; see _Process.terminate
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
        # Which mode start() launched this session in, so a crash can describe
        # what actually stopped instead of always saying "mirroring".
        self.mode = MIRROR
        # Set once STREAMING has actually been reported. The pump only treats a
        # process exit as a crash after that; before it, _await_ready owns the
        # outcome, so a startup failure cannot be overwritten by a late
        # STREAMING emit.
        self.streaming = False
        self.pump: asyncio.Task | None = None
        # What this session's start() set up in the environment, so teardown
        # undoes exactly what this session caused and never a sibling
        # session's -- see AirPlayBackend._restore_environment.
        self.virtual: str | None = None
        self.switched_display = False

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
        # Extend is limited to one session at a time (see start()); mirror can
        # run several concurrently. What each session set up in the
        # environment lives on the _Session itself, not here -- a
        # backend-scalar "the" virtual output or "the" switched display broke
        # as soon as mirror and extend could be active together.
        self._sessions: dict[str, _Session] = {}
        # Serialises the whole guard-to-registration region of an extend start;
        # see start(). Mirror starts never take it.
        self._extend_lock = asyncio.Lock()

    # -- process construction --------------------------------------------

    def build_argv(self, device: Device, mode: str = MIRROR) -> list[str]:
        argv = [
            BIN,
            "-target", device.address,
            "-port-range", self._config.airplay_port_range,
            "-fps", str(self._config.fps),
            "-hwaccel", HWACCEL_MAP.get(self._config.encoder, "auto"),
        ]
        if self._config.airplay_bitrate:
            argv += ["-bitrate", str(self._config.airplay_bitrate)]

        # Each mode keeps its own portal restore token, i.e. its own output.
        path = creds_path(mode)
        if path is not None:
            argv += ["-creds", str(path)]
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

    async def start(self, device: Device, mode: str = MIRROR) -> None:
        if mode == EXTEND:
            # The guard reads self._sessions, but a session is registered only
            # after `await self._spawn(...)`, and create_subprocess_exec yields
            # several times. Two extends racing into that window both passed
            # the guard, both ran the synchronous cleanup_strays() -- the
            # second deleting the first's live output -- and both registered
            # as "the" extend. The lock holds the reservation from the guard
            # all the way to the registration, so the loser sees the winner.
            async with self._extend_lock:
                session = await self._launch(device, mode)
        else:
            session = await self._launch(device, mode)

        # Deliberately outside the lock: this waits up to ready_timeout for the
        # child, and holding the extend slot for 30s would stall a restart of
        # the very session that owns it.
        await self._await_ready(session, initial=True)

    async def _launch(self, device: Device, mode: str) -> _Session:
        """Guard, prepare the environment, spawn the child, register the
        session -- everything that has to happen atomically for an extend."""
        # The guard runs first, before anything is torn down. It used to sit
        # after the teardown below, so a refused request had already killed the
        # requesting device's live mirror by the time it was told "already
        # extending to <some other device>" -- a working cast destroyed, with
        # nothing in the error to explain it.
        #
        # Only one virtual output is ever created, and cleanup_strays() would
        # happily tear one down out from under its session, so a second extend
        # is rejected rather than stealing the first one's output. Re-extending
        # the device that already owns the output is a restart, not a steal, so
        # it is allowed through.
        if mode == EXTEND:
            existing = self._active_extend_session()
            if existing is not None and existing.device.id != device.id:
                message = (
                    f"already extending to {existing.device.name}; stop it first"
                )
                self._emit(device, SessionState.FAILED, message)
                raise BackendError(message)

        await self._teardown(device.id)
        self._emit(device, SessionState.CONNECTING)

        virtual_name: str | None = None
        switched_display = False

        if mode == EXTEND:
            virtual_display.cleanup_strays()
            virtual_name = virtual_display.create()
            if virtual_name is None:
                message = (
                    "could not create a virtual display for extend mode; "
                    "is this Hyprland with hyprctl available?"
                )
                self._emit(device, SessionState.FAILED, message)
                raise BackendError(message)
        elif self._config.airplay_auto_resolution:
            # Mirror only: the receiver rejects a stream whose SPS does not
            # match the negotiated 1920x1080. A virtual output is already 1080p.
            display.apply_stream_mode()
            switched_display = True

        try:
            proc = await self._spawn(self.build_argv(device, mode), self.daemon_env())
        except FileNotFoundError as exc:
            # No session was ever created to hang this cleanup off of, so it
            # has to happen here or a spawn failure strands a real monitor
            # (extend) or leaves the panel stuck at 1080p (mirror) forever.
            self._undo_setup(virtual_name)
            message = f"{BIN} is not installed; install it with: yay -S doubletake"
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message) from exc
        except Exception as exc:
            self._undo_setup(virtual_name)
            message = f"{BIN} failed to start: {exc}"
            self._emit(device, SessionState.FAILED, message)
            raise

        session = _Session(device, proc)
        session.mode = mode
        session.virtual = virtual_name
        session.switched_display = switched_display
        self._sessions[device.id] = session
        session.pump = asyncio.create_task(self._pump(session))
        return session

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
        leftover = await self._teardown(device.id)
        self._emit(device, SessionState.IDLE)
        if leftover is not None:
            # The cast really did stop, so IDLE above is honest -- but the
            # desktop is not back to how it was found, and stop used to answer
            # {"ok": true, "stopped": 1} while a phantom 1080p monitor sat
            # there. Reporting success without having achieved the effect is
            # the bug this project keeps re-shipping; say so instead.
            raise BackendError(
                f"stopped casting to {device.name}, but the virtual output "
                f"{leftover!r} could not be removed. Remove it with: "
                f"hyprctl output remove {leftover}"
            )

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
                self._restore_environment(session)
                verb = "extending to" if session.mode == EXTEND else "mirroring to"
                message = (
                    f"{verb} {session.device.name} stopped unexpectedly "
                    f"({session.tail(120) or 'process exited'})"
                )
                # Logged as well as emitted: the emit becomes a desktop
                # notification that disappears, and daemon.log was then the
                # only record of the session -- with nothing in it explaining
                # why the session ended. That gap was hit while testing this
                # very branch on hardware.
                log.warning("%s", message)
                self._emit(session.device, SessionState.FAILED, message)

    async def _fail(self, session: _Session, message: str) -> None:
        await self._teardown(session.device.id)
        self._emit(session.device, SessionState.FAILED, message)

    def _active_extend_session(self) -> _Session | None:
        return next(
            (s for s in self._sessions.values() if s.virtual is not None), None
        )

    def _any_session_needs_display(self) -> bool:
        return any(s.switched_display for s in self._sessions.values())

    def _maybe_restore_display(self) -> None:
        # Safe to call whenever: restore_mode() is a no-op when nothing was
        # ever switched. Gated on whether any *remaining* session still needs
        # 1920x1080 -- a live mirror session must not have the panel pulled
        # out from under it just because some other session tore down.
        if self._config.airplay_auto_resolution and not self._any_session_needs_display():
            display.restore_mode()

    def _remove_virtual(self, name: str | None) -> str | None:
        """Remove `name` if there is one. Returns the name still on the desktop
        when the removal failed, so callers can stop claiming success."""
        if name is None:
            return None
        if virtual_display.remove(name):
            return None
        log.warning(
            "virtual output %s could not be removed and is still on the "
            "desktop; remove it with: hyprctl output remove %s", name, name,
        )
        return name

    def _undo_setup(self, virtual_name: str | None) -> str | None:
        """Undo environment changes from a start() that failed before a
        session existed to hang them off of (see the spawn try/except)."""
        leftover = self._remove_virtual(virtual_name)
        self._maybe_restore_display()
        return leftover

    def _restore_environment(self, session: _Session) -> str | None:
        """Undo exactly what `session` set up in start() -- never a sibling
        session's environment, since mirror and extend can be active at once."""
        leftover = self._remove_virtual(session.virtual)
        self._maybe_restore_display()
        return leftover

    async def _teardown(self, device_id: str) -> str | None:
        """Tear the session down. Returns the name of a virtual output that
        survived the teardown, or None when nothing was left behind."""
        session = self._sessions.pop(device_id, None)
        if session is None:
            # This device never had a session, but a crash on a previous run
            # can still have left the display switched (see
            # display.restore_mode's docstring); clear that regardless.
            self._maybe_restore_display()
            return None
        session.stopping = True
        session.proc.terminate()
        if session.pump is not None:
            session.pump.cancel()
            try:
                # Bounded: an unbounded await here hung daemon shutdown and left
                # doubletake running with nothing owning it.
                await asyncio.wait_for(asyncio.shield(session.pump), timeout=2.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        return self._restore_environment(session)
