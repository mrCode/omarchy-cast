import asyncio
import contextlib
import logging
import os
import time

from omarchy_cast.backends.base import Backend, BackendError
from omarchy_cast.core.device import PROTOCOLS, Device
from omarchy_cast.core.protocol import (
    decode_line,
    encode_response,
    err,
    ok,
    socket_path,
)
from omarchy_cast.core.session import Session, SessionState

log = logging.getLogger(__name__)

DEFAULT_PORTS = {"airplay": 7000, "cast": 8009}


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
    ) -> None:
        self.discovery = discovery
        self.backends = dict(backends)
        self.sessions: dict[str, Session] = {}
        self.idle_timeout = idle_timeout
        self._last_active = time.monotonic()
        self._stopping = asyncio.Event()

    # -- state callback given to backends ------------------------------

    def on_state(self, device: Device, state: SessionState, error: str | None) -> None:
        session = self.sessions.get(device.id)
        if session is None:
            session = Session(device)
            self.sessions[device.id] = session
        session.transition(state, error)
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
        device = self._find_device(device_id)
        if device is None:
            return err(f"device not found: {device_id}")

        backend = self.backends.get(device.protocol)
        if backend is None:
            return err(f"no backend for protocol: {device.protocol}")

        await backend.start(device)
        session = self.sessions.get(device.id)
        return ok({"state": str(session.state) if session else "idle"})

    async def _cmd_stop(self, request: dict) -> dict:
        device_id = request.get("device_id")
        if device_id is None:
            targets = list(self.sessions.values())
        elif device_id in self.sessions:
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
        if session is None:
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
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _idle_watchdog(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(1.0)
            if any(s.is_active for s in self.sessions.values()):
                self._last_active = time.monotonic()
                continue
            if time.monotonic() - self._last_active > self.idle_timeout:
                log.info("idle for %.0fs, exiting", self.idle_timeout)
                self._stopping.set()

    async def serve(self, path=None) -> None:
        path = path or socket_path()
        if path.exists():
            path.unlink()

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
                await backend.shutdown()
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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    daemon = Daemon(Discovery(), {}, idle_timeout=args.idle_timeout)
    daemon.backends["airplay"] = AirPlayBackend(daemon.on_state, config)
    daemon.backends["cast"] = CastBackend(daemon.on_state, config)

    asyncio.run(daemon.serve())


if __name__ == "__main__":
    main()
