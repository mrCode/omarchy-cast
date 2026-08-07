"""xdg-desktop-portal ScreenCast session handling.

CreateSession -> SelectSources -> Start -> OpenPipeWireRemote, with a stored
restore token suppressing the second prompt.

The portal answers over D-Bus *signals*, and Gio dispatches those through the
GLib main context -- which only runs if something iterates it. The prototype
this was ported from ran a GLib main loop; the asyncio port dropped it and
nothing replaced it, so the Response signal was never delivered. Every Cast
start hung at CreateSession until the 120s timeout, which is to say Chromecast
could not work at all. It went unnoticed because every test here stubs the
portal, so the suite stayed green.

`glib_pump()` is the missing piece: it drains GLib from the asyncio loop for
the duration of the handshake.
"""

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# How often to drain GLib while waiting on the portal. This runs only during a
# handshake, never in the background, so the poll is not an ongoing cost.
GLIB_POLL = 0.01

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

CURSOR_MODE_EMBEDDED = 2
SOURCE_TYPE_MONITOR = 1
PERSIST_MODE_PERSISTENT = 2

PORTAL_TIMEOUT = 120.0


class PortalError(Exception):
    pass


@dataclass(frozen=True)
class PortalSession:
    fd: int
    node_id: int


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "omarchy-cast"


def restore_token_path() -> Path:
    return state_dir() / "portal-restore-token"


def load_restore_token() -> str | None:
    path = restore_token_path()
    if not path.exists():
        return None
    return path.read_text().strip() or None


def save_restore_token(token: str) -> None:
    path = restore_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    path.chmod(0o600)


def parse_streams(streams) -> int:
    """Portal returns a(ua{sv}) -- a list of (node_id, properties) pairs."""
    for entry in streams:
        return int(entry[0])
    raise PortalError("portal returned no stream; screen capture was cancelled")


@contextlib.asynccontextmanager
async def glib_pump():
    """Dispatch GLib main-context events while the block runs.

    Yields the pump task so callers (and tests) can see it stop. Iterating the
    context from the asyncio loop keeps everything on one thread, so the
    portal callbacks resolve futures on the loop that is awaiting them.
    """
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import GLib

    context = GLib.MainContext.default()

    async def pump() -> None:
        while True:
            # A callback that raises must not kill the pump: the portal
            # handshake would then hang exactly as it did before this existed.
            try:
                while context.pending():
                    context.iteration(False)
            except Exception:
                log.debug("GLib callback raised during pump", exc_info=True)
            await asyncio.sleep(GLIB_POLL)

    task = asyncio.create_task(pump())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def open_screencast() -> PortalSession:
    """Open a ScreenCast session and return the PipeWire fd and node id.

    Requires a running compositor and an xdg-desktop-portal backend. Replays a
    stored restore token when present so the user is not prompted again.
    """
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None,
        BUS_NAME, OBJECT_PATH, SCREENCAST_IFACE, None,
    )

    loop = asyncio.get_running_loop()
    pending: asyncio.Future = loop.create_future()

    def on_response(_conn, _sender, _path, _iface, _signal, params):
        code, payload = params.unpack()
        if pending.done():
            return
        if code != 0:
            loop.call_soon_threadsafe(
                pending.set_exception,
                PortalError("screen capture permission denied or cancelled"),
            )
        else:
            loop.call_soon_threadsafe(pending.set_result, payload)

    subscription = bus.signal_subscribe(
        BUS_NAME, REQUEST_IFACE, "Response",
        None, None, Gio.DBusSignalFlags.NONE, on_response,
    )

    async def call(method: str, args) -> dict:
        nonlocal pending
        pending = loop.create_future()
        proxy.call_sync(method, args, Gio.DBusCallFlags.NONE, -1, None)
        return await asyncio.wait_for(pending, timeout=PORTAL_TIMEOUT)

    try:
        # Every await below is waiting on a D-Bus signal that GLib will only
        # deliver while its main context is being iterated. Without this the
        # first call never returns and the whole handshake times out.
        async with glib_pump():
            payload = await call(
                "CreateSession",
                GLib.Variant("(a{sv})", ({
                    "session_handle_token": GLib.Variant("s", "omarchycast"),
                },)),
            )
            session_handle = payload["session_handle"]

            options = {
                "types": GLib.Variant("u", SOURCE_TYPE_MONITOR),
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", CURSOR_MODE_EMBEDDED),
                "persist_mode": GLib.Variant("u", PERSIST_MODE_PERSISTENT),
            }
            stored = load_restore_token()
            if stored:
                options["restore_token"] = GLib.Variant("s", stored)

            await call(
                "SelectSources", GLib.Variant("(oa{sv})", (session_handle, options))
            )
            payload = await call(
                "Start", GLib.Variant("(osa{sv})", (session_handle, "", {}))
            )
    except TimeoutError as exc:
        raise PortalError(
            f"portal did not respond within {PORTAL_TIMEOUT:.0f}s"
        ) from exc
    finally:
        bus.signal_unsubscribe(subscription)

    if payload.get("restore_token"):
        save_restore_token(payload["restore_token"])

    node_id = parse_streams(payload.get("streams") or [])

    _, fd_list = proxy.call_with_unix_fd_list_sync(
        "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (session_handle, {})),
        Gio.DBusCallFlags.NONE, -1, None, None,
    )
    fds = fd_list.steal_fds()
    if not fds:
        raise PortalError("portal returned no PipeWire file descriptor")
    return PortalSession(fd=fds[0], node_id=node_id)
