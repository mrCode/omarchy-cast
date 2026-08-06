import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time

from collections.abc import Callable

from omarchy_cast.backends.base import Backend, BackendError
from omarchy_cast.backends.creds import MIRROR, MODES
from omarchy_cast.core.device import PROTOCOLS, Device
from omarchy_cast.core.protocol import (
    decode_line,
    encode_response,
    err,
    ok,
    socket_path,
)
from omarchy_cast.core.session import InvalidTransition, Session, SessionState

log = logging.getLogger(__name__)

DEFAULT_PORTS = {"airplay": 7000, "cast": 8009}

# The Cast backend has never been exercised against real hardware -- it is
# covered only by unit tests against fakes. Say so at the point of use rather
# than only in the README, so nobody debugs it thinking it is known-good.
CAST_UNTESTED = (
    "Chromecast support is UNTESTED against real hardware and may not work. "
    "If you try it, please report the result: "
    "https://github.com/mrCode/omarchy-cast/issues"
)


def desktop_notify(message: str) -> None:
    """Surface a failure via mako. Best effort; never raises."""
    try:
        subprocess.run(
            ["notify-send", "-u", "critical", "omarchy-cast", message],
            check=False,
        )
    except OSError:
        log.debug("notify-send unavailable", exc_info=True)


def _device_dict(d: Device) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "protocol": d.protocol,
        "address": d.address,
        "model": d.model,
    }


class Daemon:
    def __init__(
        self,
        discovery,
        backends: dict[str, Backend],
        idle_timeout: float = 30.0,
        notifier: Callable[[str], None] | None = None,
    ) -> None:
        self.discovery = discovery
        self.backends = dict(backends)
        self.sessions: dict[str, Session] = {}
        self.idle_timeout = idle_timeout
        self._notify = notifier or desktop_notify
        self._last_active = time.monotonic()
        self._stopping = asyncio.Event()

    # -- state callback given to backends ------------------------------

    def on_state(self, device: Device, state: SessionState, error: str | None) -> None:
        session = self.sessions.get(device.id)
        if session is None:
            # _cmd_start registers the session (with its mode) before calling
            # the backend, so reaching here means a stray emit for a device
            # with no active start call -- e.g. a late crash emit after the
            # session was already popped. Its mode is unknowable; default it.
            session = Session(device, mode=MIRROR)
            self.sessions[device.id] = session

        try:
            session.transition(state, error)
        except InvalidTransition:
            # Backends emit from background tasks (the AirPlay supervisor among
            # them). A late or duplicate emit must not take that task down.
            log.debug("ignoring %s -> %s for %s", session.state, state, device.id)
            return

        if state is SessionState.FAILED:
            # waybar may be collapsed into the tray, so push it to the user too.
            try:
                self._notify(error or f"casting to {device.name} failed")
            except Exception:
                log.debug("notifier raised", exc_info=True)

        # Terminal states are not retained; a failed session must not block a retry.
        if state in (SessionState.IDLE, SessionState.FAILED):
            self.sessions.pop(device.id, None)
        self._last_active = time.monotonic()

    # -- request handling ----------------------------------------------

    def _find_device(self, device_id: str) -> Device | None:
        for device in self.discovery.devices():
            if device.id == device_id:
                return device
        for session in self.sessions.values():
            if session.device.id == device_id:
                return session.device
        return None

    async def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            return err(f"unknown command: {cmd}")
        try:
            return await handler(request)
        except BackendError as exc:
            # Logged as well as returned: a client that disconnects mid-request
            # would otherwise take the only explanation with it.
            log.error("%s failed: %s", cmd, exc)
            return err(str(exc))

    async def _cmd_list(self, request: dict) -> dict:
        return ok({"devices": [_device_dict(d) for d in self.discovery.devices()]})

    async def _cmd_status(self, request: dict) -> dict:
        sessions = [
            {
                "id": s.device.id,
                "name": s.device.name,
                "protocol": s.device.protocol,
                "state": str(s.state),
                "mode": s.mode,
                "error": s.error,
            }
            for s in self.sessions.values()
        ]
        return ok({"sessions": sessions})

    async def _cmd_add(self, request: dict) -> dict:
        """Register a device by raw address.

        Required because some access points do not forward multicast, leaving a
        perfectly reachable receiver invisible to mDNS.
        """
        address = request.get("address")
        if not address:
            return err("add requires an address")

        protocol = request.get("protocol", "airplay")
        if protocol not in PROTOCOLS:
            return err(f"unknown protocol: {protocol}; expected one of {PROTOCOLS}")

        device = Device(
            id=Device.make_id(protocol, address),
            name=request.get("name") or address,
            address=address,
            port=int(request.get("port") or DEFAULT_PORTS[protocol]),
            protocol=protocol,
        )
        self.discovery.add(device)
        return ok({"device": _device_dict(device)})

    async def _cmd_start(self, request: dict) -> dict:
        device_id = request.get("device_id")
        mode = request.get("mode", MIRROR)
        if mode not in MODES:
            return err(f"unknown mode: {mode}; expected one of {MODES}")

        device = self._find_device(device_id)
        if device is None:
            return err(f"device not found: {device_id}")

        backend = self.backends.get(device.protocol)
        if backend is None:
            return err(f"no backend for protocol: {device.protocol}")

        # Register the session -- with its mode -- before calling the backend,
        # rather than stashing the mode in shared daemon state. backend.start()
        # can suspend before its first emit (AirPlay awaits a teardown that can
        # take up to 2s), and the daemon serves clients concurrently, so a
        # second in-flight start could otherwise overwrite a "pending mode"
        # before the first device's session was ever created from it.
        session = Session(device, mode=mode)
        self.sessions[device.id] = session
        try:
            await backend.start(device, mode)
        except Exception:
            # A start that raises without the backend ever reaching a terminal
            # state (FAILED emits pop the session themselves) must not leave a
            # never-transitioned session behind to block a retry.
            if self.sessions.get(device.id) is session and session.state is SessionState.IDLE:
                self.sessions.pop(device.id, None)
            raise

        session = self.sessions.get(device.id)
        data = {"state": str(session.state) if session else "idle", "mode": mode}
        if device.protocol == "cast":
            data["warning"] = CAST_UNTESTED
            log.warning(CAST_UNTESTED)
        return ok(data)

    async def _cmd_stop(self, request: dict) -> dict:
        device_id = request.get("device_id")
        # A session in IDLE has been registered by _cmd_start but its backend
        # has not emitted anything yet (e.g. AirPlay's pre-start teardown,
        # which can take up to 2s). backend.stop() on it would be a no-op the
        # backend doesn't recognise, and on_state would silently swallow the
        # resulting illegal transitions -- so it must not count as stoppable.
        if device_id is None:
            targets = [
                s for s in self.sessions.values() if s.state is not SessionState.IDLE
            ]
        elif device_id in self.sessions and self.sessions[device_id].state is not SessionState.IDLE:
            targets = [self.sessions[device_id]]
        else:
            return err(f"no active session for: {device_id}")

        for session in targets:
            backend = self.backends.get(session.device.protocol)
            if backend is not None:
                await backend.stop(session.device)
        return ok({"stopped": len(targets)})

    async def _cmd_pin(self, request: dict) -> dict:
        device_id = request.get("device_id")
        session = self.sessions.get(device_id)
        # Same reasoning as _cmd_stop: an IDLE session has no backend-side
        # counterpart yet, so there is nothing to submit a PIN to.
        if session is None or session.state is SessionState.IDLE:
            return err(f"no pending session for: {device_id}")
        backend = self.backends[session.device.protocol]
        await backend.submit_pin(session.device, str(request.get("pin", "")))
        return ok({"state": str(session.state)})

    async def _cmd_quit(self, request: dict) -> dict:
        self._stopping.set()
        return ok({"quitting": True})

    # -- serving ---------------------------------------------------------

    async def _on_client(self, reader, writer) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                request = decode_line(line)
            except ValueError as exc:
                response = err(str(exc))
            else:
                response = await self.handle(request)
            writer.write(encode_response(response))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            # The client went away before reading the reply -- routine when a
            # UI cancels a request. Not worth a traceback.
            log.debug("client disconnected before reading the response")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _on_signal(self, sig) -> None:
        log.info("received %s, shutting down", getattr(sig, "name", sig))
        self._stopping.set()

    async def _idle_watchdog(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(1.0)
            if any(s.is_active for s in self.sessions.values()):
                self._last_active = time.monotonic()
                continue
            if time.monotonic() - self._last_active > self.idle_timeout:
                log.info(
                    "idle for %.0fs, exiting (sessions=%s)",
                    self.idle_timeout,
                    {k: str(v.state) for k, v in self.sessions.items()} or "none",
                )
                self._stopping.set()

    async def serve(self, path=None) -> None:
        path = path or socket_path()
        if path.exists():
            path.unlink()

        # Without these, SIGTERM kills the process outright and the cleanup
        # below never runs -- leaving doubletake and its capture pipelines
        # alive with nothing owning them. Logout, reboot and pkill all send it.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal, sig)

        self.discovery.start()
        server = await asyncio.start_unix_server(self._on_client, path=str(path))
        os.chmod(path, 0o600)
        watchdog = asyncio.create_task(self._idle_watchdog())
        try:
            await self._stopping.wait()
        finally:
            watchdog.cancel()
            server.close()
            await server.wait_closed()
            for backend in self.backends.values():
                try:
                    # Never let one backend's shutdown strand the others'
                    # children; a hang here previously orphaned doubletake.
                    await asyncio.wait_for(backend.shutdown(), timeout=5.0)
                except Exception:
                    log.warning("backend %s did not shut down cleanly", backend.protocol)
            self.discovery.stop()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def main() -> None:
    import argparse

    from omarchy_cast.backends.airplay import AirPlayBackend
    from omarchy_cast.backends.cast import CastBackend
    from omarchy_cast.core.config import load_config
    from omarchy_cast.core.discovery import Discovery

    parser = argparse.ArgumentParser(prog="omarchy-castd")
    parser.add_argument("--idle-timeout", type=float, default=30.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # The daemon is usually auto-spawned with its pipes sent to /dev/null, so
    # a log file is the only way to diagnose anything after the fact.
    from omarchy_cast.core.display import state_dir

    log_dir = state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "daemon.log"),
            logging.StreamHandler(),
        ],
    )

    # A previous run may have died mid-cast with the display still switched.
    from omarchy_cast.core import display, virtual_display
    if display.restore_mode():
        log.info("restored a display mode left over from a previous session")
    strays = virtual_display.cleanup_strays()
    if strays:
        log.info("removed %d virtual output(s) left over from a previous session", strays)

    config = load_config()
    daemon = Daemon(Discovery(), {}, idle_timeout=args.idle_timeout)
    daemon.backends["airplay"] = AirPlayBackend(daemon.on_state, config)
    daemon.backends["cast"] = CastBackend(daemon.on_state, config)

    asyncio.run(daemon.serve())


if __name__ == "__main__":
    main()
