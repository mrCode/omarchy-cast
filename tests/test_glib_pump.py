"""The GLib main context must be dispatched, or the portal never answers.

Gio delivers D-Bus signal callbacks through the GLib main context. Nothing in
this package ever iterated it, so the ScreenCast Response signal was never
dispatched: every Cast start hung at CreateSession until the 120s portal
timeout. Chromecast could not have worked, and the suite stayed green because
every portal test stubs the portal.

These tests use real GLib -- no portal, no compositor, no network.
"""

import asyncio

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gio", "2.0")
from gi.repository import GLib  # noqa: E402

from omarchy_cast.capture.portal import glib_pump  # noqa: E402


async def test_a_glib_callback_never_runs_without_the_pump():
    """The bug, stated as a test: this is what the code used to do."""
    fired = []
    GLib.idle_add(lambda: fired.append(True) and False)

    await asyncio.sleep(0.3)

    assert fired == [], "if this fires on its own, the pump is not what fixes it"


async def test_the_pump_dispatches_glib_callbacks():
    fired = []
    GLib.idle_add(lambda: fired.append(True) and False)

    async with glib_pump():
        await asyncio.sleep(0.3)

    assert fired == [True]


async def test_a_future_resolved_from_a_glib_callback_completes():
    """Exactly the portal's shape: a Gio callback resolves the future that the
    portal handshake is awaiting."""
    loop = asyncio.get_running_loop()
    pending = loop.create_future()

    def from_glib():
        if not pending.done():
            loop.call_soon_threadsafe(pending.set_result, "portal response")
        return False

    GLib.idle_add(from_glib)

    async with glib_pump():
        assert await asyncio.wait_for(pending, timeout=3.0) == "portal response"


async def test_the_pump_stops_when_it_is_done():
    """It must not leave a task spinning for the life of the daemon."""
    async with glib_pump() as task:
        await asyncio.sleep(0.05)
        assert not task.done()

    assert task.done()


async def test_the_pump_survives_a_callback_that_raises():
    """One bad GLib callback must not stop the portal handshake."""
    def boom():
        raise RuntimeError("callback exploded")

    fired = []
    GLib.idle_add(boom)
    GLib.idle_add(lambda: fired.append(True) and False)

    async with glib_pump():
        await asyncio.sleep(0.3)

    assert fired == [True]
