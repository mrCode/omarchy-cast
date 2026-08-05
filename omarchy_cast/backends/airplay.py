import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable

from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState

log = logging.getLogger(__name__)

CommandRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

DAEMON_BIN = "doubletake"
CTL_BIN = "doubletake-ctl"

# Verified against internal/daemon/daemon.go and a live AppleTV11,1.
DT_STATES = ("idle", "discovering", "connecting", "streaming", "pin_required")

STATE_MAP = {
    "idle": SessionState.IDLE,
    "discovering": SessionState.CONNECTING,
    "connecting": SessionState.CONNECTING,
    "streaming": SessionState.STREAMING,
    "pin_required": SessionState.AWAITING_PIN,
}

CONNECT_TIMEOUT = 30.0
SUPERVISE_INTERVAL = 3.0


def parse_ctl(stdout: str) -> dict:
    """Parse a doubletake-ctl JSON envelope."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BackendError(
            f"unexpected output from {CTL_BIN}: {stdout.strip()[:120]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise BackendError(f"unexpected output from {CTL_BIN}: not an object")
    return payload


async def subprocess_runner(argv: list[str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


class AirPlayBackend(Backend):
    """Delegates AirPlay mirroring to the external doubletake daemon.

    doubletake owns its own screen capture; this backend only supervises the
    process and drives it through doubletake-ctl.
    """

    protocol = "airplay"

    def __init__(
        self,
        on_state: StateCallback,
        config: Config,
        runner: CommandRunner | None = None,
        poll_interval: float = 0.5,
        connect_timeout: float = CONNECT_TIMEOUT,
        supervise_interval: float = SUPERVISE_INTERVAL,
    ) -> None:
        super().__init__(on_state)
        self._config = config
        self._run = runner or subprocess_runner
        self._poll_interval = poll_interval
        self._connect_timeout = connect_timeout
        self._supervise_interval = supervise_interval
        self._daemon_started = False
        self._supervisors: dict[str, asyncio.Task] = {}

    def daemon_env(self) -> dict[str, str]:
        """Environment for the doubletake daemon.

        DOUBLETAKE_CODE is plumbed ahead of upstream #26 landing, so password
        support becomes a config change rather than a code change.
        """
        env = dict(os.environ)
        if self._config.airplay_code:
            env["DOUBLETAKE_CODE"] = self._config.airplay_code
        else:
            env.pop("DOUBLETAKE_CODE", None)
        return env

    async def _exec(self, argv: list[str]) -> tuple[int, str, str]:
        try:
            return await self._run(argv)
        except FileNotFoundError as exc:
            raise BackendError(
                f"{DAEMON_BIN} is not installed; install it with: yay -S doubletake"
            ) from exc

    async def _ensure_daemon(self) -> None:
        if self._daemon_started:
            return
        argv = [DAEMON_BIN, "-daemonize", "-port-range", self._config.airplay_port_range]
        if self._config.airplay_bitrate:
            argv += ["-bitrate", str(self._config.airplay_bitrate)]
        code, _, stderr = await self._exec(argv)
        if code != 0 and "already running" not in stderr.lower():
            raise BackendError(f"could not start {DAEMON_BIN}: {stderr.strip() or code}")
        self._daemon_started = True

    def _explain(self, device: Device, detail: str) -> str:
        # Root cause confirmed against doubletake 0.4.0 source and a live
        # AppleTV11,1: daemon.Config carries no PortMin/PortMax, so -port-range
        # is silently ignored in -daemonize mode and the receiver's reverse
        # handshake lands on ephemeral ports a default-DROP firewall discards.
        # SETUP then stalls (or returns 401) and mirroring never starts.
        # The same device mirrors fine via a direct `doubletake -target` run,
        # where the flag is honoured.
        if "401" in detail or "timeout" in detail.lower():
            return (
                f"{device.name} never completed SETUP. The receiver connects back "
                f"to this machine, and doubletake 0.4.0 ignores -port-range when "
                f"running as a daemon (daemon.Config has no port fields), so it "
                f"listens on random ephemeral ports instead of "
                f"{self._config.airplay_port_range}. A default-DROP firewall then "
                f"drops the receiver's connection. Either allow inbound TCP+UDP "
                f"from {device.address} on the ephemeral range, or run "
                f"'doubletake -target {device.address}' directly, where "
                f"-port-range works. ({detail})"
            )
        return (
            f"could not reach {device.name}. The receiver connects back to this "
            f"machine, so inbound TCP and UDP on ports "
            f"{self._config.airplay_port_range} must be allowed -- a default-DROP "
            f"firewall makes this stall silently. ({detail})"
        )

    async def _ctl(self, args: list[str]) -> dict:
        """Run doubletake-ctl and return its parsed envelope."""
        _, stdout, stderr = await self._exec([CTL_BIN, *args])
        payload = parse_ctl(stdout)
        if not payload.get("ok", False):
            raise BackendError(payload.get("error") or stderr.strip() or "command failed")
        return payload

    async def start(self, device: Device) -> None:
        self._emit(device, SessionState.CONNECTING)
        await self._ensure_daemon()

        # connect is asynchronous: it returns state="connecting" straight away.
        try:
            await self._ctl(["connect", device.address])
        except BackendError as exc:
            message = self._explain(device, str(exc))
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message) from exc

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._connect_timeout
        while loop.time() < deadline:
            payload = await self._ctl(["status"])
            state = STATE_MAP.get(payload.get("state", ""), SessionState.CONNECTING)

            if state is SessionState.AWAITING_PIN:
                self._emit(device, SessionState.AWAITING_PIN)
                return
            if state is SessionState.STREAMING:
                self._emit(device, SessionState.STREAMING)
                self._supervise(device)
                return

            await asyncio.sleep(self._poll_interval)

        message = self._explain(
            device, f"never reached streaming within {self._connect_timeout:.0f}s"
        )
        self._emit(device, SessionState.FAILED, message)
        raise BackendError(message)

    async def submit_pin(self, device: Device, pin: str) -> None:
        # doubletake-ctl pin takes only the PIN; the daemon knows the device.
        try:
            await self._ctl(["pin", pin])
        except BackendError as exc:
            message = f"pairing failed: {exc}"
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message) from exc
        self._emit(device, SessionState.STREAMING)

    # -- supervision -----------------------------------------------------

    def _supervise(self, device: Device) -> None:
        """Watch a live stream so a doubletake crash does not go unnoticed.

        Without this the session stays STREAMING forever after the process
        dies: waybar keeps showing green and stop has nothing to stop.
        """
        self._cancel_supervisor(device.id)
        self._supervisors[device.id] = asyncio.create_task(self._watch(device))

    def _cancel_supervisor(self, device_id: str) -> None:
        task = self._supervisors.pop(device_id, None)
        if task is not None:
            task.cancel()

    async def _watch(self, device: Device) -> None:
        try:
            while True:
                await asyncio.sleep(self._supervise_interval)
                try:
                    payload = await self._ctl(["status"])
                except BackendError as exc:
                    self._drop(device, str(exc))
                    return

                state = STATE_MAP.get(payload.get("state", ""))
                # AWAITING_PIN can legitimately last minutes.
                if state in (SessionState.STREAMING, SessionState.AWAITING_PIN):
                    continue

                self._drop(device, f"doubletake reported {payload.get('state')!r}")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let this escape into the event loop
            log.debug("supervisor error", exc_info=True)
            self._drop(device, str(exc))

    def _drop(self, device: Device, detail: str) -> None:
        self._supervisors.pop(device.id, None)
        self._emit(
            device,
            SessionState.FAILED,
            f"mirroring to {device.name} stopped unexpectedly ({detail})",
        )

    # -- teardown --------------------------------------------------------

    async def stop(self, device: Device) -> None:
        self._cancel_supervisor(device.id)
        self._emit(device, SessionState.STOPPING)
        await self._exec([CTL_BIN, "disconnect", device.address])
        self._emit(device, SessionState.IDLE)

    async def shutdown(self) -> None:
        for device_id in list(self._supervisors):
            self._cancel_supervisor(device_id)
        if self._daemon_started:
            await self._exec([CTL_BIN, "disconnect"])
