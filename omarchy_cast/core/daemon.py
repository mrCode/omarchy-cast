import asyncio
import contextlib
import logging
import os
import signal
import time

from collections.abc import Callable

from omarchy_cast.backends.base import Backend, BackendError, BackendRefused
from omarchy_cast.backends.cast import CAST_DISABLED
from omarchy_cast.core import manual
from omarchy_cast.core.device import PROTOCOLS, Device
from omarchy_cast.core.notify import notify
from omarchy_cast.core.protocol import (
    decode_line,
    encode_response,
    err,
    ok,
    socket_path,
)
from omarchy_cast.core.session import (
    MIRROR,
    MODES,
    InvalidTransition,
    Session,
    SessionState,
)

log = logging.getLogger(__name__)

DEFAULT_PORTS = {"airplay": 7000, "cast": 8009}

# How long `list` will wait for a just-started mDNS browser to hear its first
# reply. Receivers on a healthy network answer well inside this; the ceiling is
# what a user waits at the keybind before the menu opens, so it stays short.
# The daemon exiting throws away the mDNS cache, and a cold browser is close
# to useless: measured on a real network, a fresh zeroconf browser took 15.7s
# for its FIRST result and found one receiver in 90 seconds, while avahi --
# running since boot with a warm cache -- listed six instantly. At the old 30s
# idle timeout almost every command started cold, which is why `list` came back
# empty and `start` said "device not found" for receivers plainly present.
# Staying resident costs a few MB and keeps discovery useful.
IDLE_TIMEOUT = 900.0

DISCOVERY_GRACE = 3.0
DISCOVERY_POLL = 0.1

# `start` names a specific receiver, so the user has already committed and
# waiting beats failing. `list` is interactive and stays snappy. Measured on a
# real network: an Apple TV took ~8s to answer a freshly started browser, so a
# 3s ceiling reported "device not found" for a receiver plainly present.
START_DISCOVERY_GRACE = 12.0

# The Cast backend has never been exercised against real hardware -- it is
# covered only by unit tests against fakes. Say so at the point of use rather
# than only in the README, so nobody debugs it thinking it is known-good.
CAST_UNTESTED = (
    "Chromecast support is UNTESTED against real hardware and may not work. "
    "If you try it, please report the result: "
    "https://github.com/mrCode/omarchy-cast/issues"
)


def desktop_notify(message: str) -> None:
    """Surface a mid-stream crash. Urgent, because nobody is watching for it."""
    notify(message, urgent=True)


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
        idle_timeout: float = IDLE_TIMEOUT,
        notifier: Callable[[str], None] | None = None,
    ) -> None:
        self.discovery = discovery
        self.backends = dict(backends)
        self.sessions: dict[str, Session] = {}
        self.idle_timeout = idle_timeout
        self._notify = notifier or desktop_notify
        self._last_active = time.monotonic()
        self._stopping = asyncio.Event()
        # Overwritten when discovery actually starts; until then every list is
        # inside the grace window, which is the correct answer for a daemon
        # that has not begun looking yet.
        self._discovery_started_at = time.monotonic()

    # -- state callback given to backends ------------------------------

    def on_state(self, device: Device, state: SessionState, error: str | None) -> None:
        session = self.sessions.get(device.id)
        synthesized = session is None
        if session is None:
            # _cmd_start registers the session (with its mode) before calling
            # the backend, so reaching here means a stray emit for a device
            # with no active start call -- e.g. a late crash emit after the
            # session was already popped. Its mode is unknowable; default it.
            session = Session(device, mode=MIRROR)
            self.sessions[device.id] = session

        was_streaming = session.state is SessionState.STREAMING

        try:
            session.transition(state, error)
        except InvalidTransition:
            # Backends emit from background tasks (the AirPlay supervisor among
            # them). A late or duplicate emit must not take that task down.
            log.debug("ignoring %s -> %s for %s", session.state, state, device.id)
            if synthesized:
                # Nothing legal ever landed on it, so this session records no
                # real cast. Leaving it would strand an entry that `status`
                # hides (it is IDLE) but `stop` would still try to tear down.
                self.sessions.pop(device.id, None)
            return

        if state is SessionState.FAILED and was_streaming:
            # Only a cast that DIED is announced here. A cast that never came
            # up fails the `start` command too, and whichever client ran it
            # reports that error itself -- notifying both produced two sticky
            # banners for one failure. A mid-stream crash has no command to
            # answer, so this is the only way the user learns the screen they
            # are presenting from went dark.
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
        """Answer with the devices known so far, giving a cold start a moment.

        mDNS is a conversation, not a lookup: the browser has to send a query
        and wait for receivers to answer, which takes a second or two. The
        daemon exits after 30s idle, so almost every `list` -- and therefore
        almost every press of the cast keybind -- spawns a fresh daemon and
        asks it immediately. Answering honestly-but-uselessly with an empty
        list made the app look like it could not see a receiver that was
        sitting right there, and sent the user hunting through firewall rules.

        The wait applies only while the browser is still young AND nothing has
        been found yet, so a genuinely empty network still answers promptly
        once the grace period is behind it, and a warm daemon never waits.
        """
        # Wait on mDNS specifically, not on "anything known". A remembered
        # device makes devices() non-empty from the first instant, which
        # skipped this wait entirely for the users most likely to need it --
        # the ones who had to add a receiver by address in the first place.
        await self._await_discovery()

        found = self.discovery.devices()
        if CAST_DISABLED:
            # Offering a receiver whose only possible outcome is an error is
            # worse than not listing it: the user picks it, waits, and gets a
            # failure for something the app already knows it will refuse.
            hidden = sum(1 for d in found if d.protocol == "cast")
            found = [d for d in found if d.protocol != "cast"]
        else:
            hidden = 0

        data = {"devices": [_device_dict(d) for d in found]}
        if hidden:
            data["hidden_cast"] = hidden
        return ok(data)

    async def _await_discovery(self, grace: float = DISCOVERY_GRACE) -> None:
        """Give a just-started mDNS browser its chance to answer.

        Shared by `list` and `start`. It lived only in `list` at first, so the
        keybind worked (menu lists, then starts against a warm daemon) while
        `omarchy-cast start <id>` against a cold one failed with "device not
        found" for a receiver that was plainly there.
        """
        deadline = self._discovery_started_at + grace
        while not self._discovery_has_answered() and time.monotonic() < deadline:
            await asyncio.sleep(DISCOVERY_POLL)

    def _discovery_has_answered(self) -> bool:
        has_discovered = getattr(self.discovery, "has_discovered", None)
        if has_discovered is None:
            return bool(self.discovery.devices())
        return has_discovered()

    async def _cmd_status(self, request: dict) -> dict:
        # IDLE is filtered out for the same reason _cmd_stop and _cmd_pin
        # refuse it: _cmd_start registers the session before calling the
        # backend, so an IDLE session is one whose backend has not emitted
        # anything yet (AirPlay's pre-start teardown can sit there for 2s).
        # Reporting it made waybar -- which has no idle branch and falls
        # through to its streaming return -- show a green streaming indicator
        # offering a Stop that _cmd_stop then refused.
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
            if s.state is not SessionState.IDLE
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
        # Remembered on disk because the daemon exits after 30s idle. A device
        # that needed --address once will need it every time -- discovery is
        # never going to start finding it -- so losing this on the next restart
        # meant retyping the address for every cast.
        if not manual.remember(device):
            log.warning("added %s but could not remember it for next time", device.id)
        return ok({"device": _device_dict(device)})

    async def _cmd_forget(self, request: dict) -> dict:
        """Drop a remembered device.

        These entries never expire on their own: an address that was right on
        one network is wrong on the next, so without a way to remove them the
        menu accumulates receivers that can never be reached.
        """
        device_id = request.get("device_id")
        if not device_id:
            return err("forget requires a device id")

        if not manual.forget(device_id):
            return err(f"not a remembered device: {device_id}")

        # Also drop it from the running daemon, so the change shows up in the
        # menu now rather than after the next restart.
        self.discovery.remove(device_id)
        return ok({"forgot": device_id})

    async def _cmd_start(self, request: dict) -> dict:
        device_id = request.get("device_id")
        mode = request.get("mode", MIRROR)
        if mode not in MODES:
            return err(f"unknown mode: {mode}; expected one of {MODES}")

        device = self._find_device(device_id)
        if device is None:
            # A cold daemon has not heard from mDNS yet. Without this, starting
            # by id against a freshly spawned daemon reported "device not
            # found" for a receiver that `list` would show a second later.
            deadline = self._discovery_started_at + START_DISCOVERY_GRACE
            while device is None and time.monotonic() < deadline:
                await asyncio.sleep(DISCOVERY_POLL)
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
        previous = self.sessions.get(device.id)
        session = Session(device, mode=mode)
        self.sessions[device.id] = session
        try:
            await backend.start(device, mode)
        except BackendRefused:
            # The backend declined without touching the device, so whatever it
            # was already doing, it still is. The record displaced above is
            # therefore still true and has to go back -- dropping it stranded a
            # live session: waybar showed "not casting" and no stop could reach
            # it. Restoring on *any* failure is wrong for the opposite reason:
            # a failed restart tears the old cast down on its way in, and
            # putting that record back claims a cast that is gone.
            if self.sessions.get(device.id) is session:
                self.sessions.pop(device.id, None)
            if previous is not None and self.sessions.get(device.id) is None:
                self.sessions[device.id] = previous
            raise
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

        # Failures are collected rather than raised through: a backend that
        # stopped the cast but could not fully clean up (AirPlay failing to
        # remove its virtual output) must still be reported, but must not
        # abandon the sessions queued behind it.
        problems = []
        for session in targets:
            backend = self.backends.get(session.device.protocol)
            if backend is None:
                continue
            try:
                await backend.stop(session.device)
            except BackendError as exc:
                log.warning("stop for %s: %s", session.device.id, exc)
                problems.append(str(exc))
        if problems:
            return err("; ".join(problems))
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
        self._discovery_started_at = time.monotonic()
        for device in manual.load():
            self.discovery.add(device)
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
    from omarchy_cast.core.avahi import AvahiDiscovery
    from omarchy_cast.core.avahi import available as avahi_available
    from omarchy_cast.core.discovery import Discovery

    parser = argparse.ArgumentParser(prog="omarchy-castd")
    parser.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT)
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

    from omarchy_cast.core import display, singleton, virtual_display

    # Before ANYTHING that touches shared state. The cleanup below cannot tell
    # a leftover virtual output from one carrying a cast that is streaming
    # right now in another daemon -- it removed the live one, and the cast died
    # with nothing in its own daemon's log, because the removal happened in a
    # different process. The client spawns a daemon whenever the socket is
    # briefly absent, so this is reachable in ordinary use.
    lock = singleton.acquire()
    if lock is None:
        log.info("another daemon is already running; exiting")
        return

    # A previous run may have died mid-cast with the display still switched.
    if display.restore_mode():
        log.info("restored a display mode left over from a previous session")
    strays = virtual_display.cleanup_strays()
    if strays:
        log.info("removed %d virtual output(s) left over from a previous session", strays)

    config = load_config()
    # Prefer avahi: it has been running since boot with a warm cache, while a
    # browser we start ourselves is cold and, measured on a real network, took
    # 15.7s for its first result. Fall back to our own stack where avahi is
    # absent, which keeps this working on systems that do not run it.
    if avahi_available():
        discovery = AvahiDiscovery()
        log.info("discovery: avahi")
    else:
        discovery = Discovery()
        log.info("discovery: built-in zeroconf (avahi not available)")

    daemon = Daemon(discovery, {}, idle_timeout=args.idle_timeout)
    daemon.backends["airplay"] = AirPlayBackend(daemon.on_state, config)
    daemon.backends["cast"] = CastBackend(daemon.on_state, config)

    try:
        asyncio.run(daemon.serve())
    finally:
        # Held until the very end; releasing it earlier would let a second
        # daemon start and sweep a live cast's virtual output.
        lock.close()


if __name__ == "__main__":
    main()
