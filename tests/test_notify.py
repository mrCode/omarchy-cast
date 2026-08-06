from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core import notify as core_notify
from omarchy_cast.core.daemon import Daemon
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


class FakeDiscovery:
    def __init__(self, devices):
        self._devices = list(devices)

    def devices(self):
        return self._devices

    def add(self, device):
        self._devices.append(device)

    def start(self):
        pass

    def stop(self):
        pass


def make_device():
    return Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")


def make_daemon(**stub_kwargs):
    sent = []
    daemon = Daemon(FakeDiscovery([make_device()]), {}, notifier=sent.append)
    daemon.backends["cast"] = StubBackend(daemon.on_state, **stub_kwargs)
    return daemon, sent


async def test_a_cast_that_dies_mid_stream_notifies_the_user():
    """Nothing else can tell them: no command is waiting on this."""
    daemon, sent = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions["cast:1"].state is SessionState.STREAMING

    daemon.on_state(make_device(), SessionState.FAILED, "mirroring stopped unexpectedly")

    assert len(sent) == 1
    assert "mirroring stopped unexpectedly" in sent[0]


async def test_a_failed_start_does_not_notify():
    """The `start` command returns this same error, and whichever client ran it
    reports it. Notifying here too gave the user two sticky banners for one
    failure -- the complaint that prompted splitting these cases apart."""
    daemon, sent = make_daemon(fail_with="mirroring stopped unexpectedly")

    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})

    assert resp["ok"] is False
    assert "mirroring stopped unexpectedly" in resp["error"]
    assert sent == []


async def test_normal_stop_does_not_notify():
    daemon, sent = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    await daemon.handle({"cmd": "stop"})
    assert sent == []


async def test_notifier_failure_does_not_break_the_daemon():
    """notify-send may not be installed; that must not take the daemon down."""

    def boom(message):
        raise OSError("notify-send missing")

    daemon = Daemon(FakeDiscovery([make_device()]), {}, notifier=boom)
    daemon.backends["cast"] = StubBackend(daemon.on_state, fail_with="boom")
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is False


async def test_session_still_cleared_after_notifying():
    daemon, _ = make_daemon(fail_with="boom")
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions == {}


async def test_late_emit_after_failure_is_ignored_not_raised():
    """Backends emit from background tasks; a late emit must not crash them."""
    daemon, sent = make_daemon(fail_with="boom")
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})

    # The session is already gone; this transition is illegal from IDLE. The
    # assertion is that this returns at all -- it used to be made indirectly
    # via a notification count, which stopped meaning anything once a failed
    # start no longer notifies.
    daemon.on_state(make_device(), SessionState.STOPPING, None)

    assert daemon.sessions == {}
    assert sent == []


# -- the notify-send invocation itself --------------------------------------
#
# Every daemon in this suite gets an injected notifier, so without these the
# argv built by core.notify is never executed by any test. That argv IS the
# fix for notification flooding: a typo in the hint string restores stacking
# silently, with the suite still green.


def _argv(monkeypatch, **kwargs):
    seen = []
    monkeypatch.setattr(
        core_notify.subprocess, "run", lambda argv, **kw: seen.append(argv)
    )
    core_notify.notify("something happened", **kwargs)
    return seen[0]


def test_normal_notifications_replace_rather_than_stack(monkeypatch):
    argv = _argv(monkeypatch)
    assert "-h" in argv
    hint = argv[argv.index("-h") + 1]
    assert hint == "string:x-canonical-private-synchronous:omarchy-cast"


def test_normal_notifications_expire_on_their_own(monkeypatch):
    argv = _argv(monkeypatch)
    assert argv[argv.index("-u") + 1] == "normal"
    assert "-t" in argv and int(argv[argv.index("-t") + 1]) > 0


def test_a_crash_is_critical_and_has_no_expiry(monkeypatch):
    """Sticky is the point here: it is how the user learns the screen they are
    presenting from went dark."""
    argv = _argv(monkeypatch, urgent=True)
    assert argv[argv.index("-u") + 1] == "critical"
    assert "-t" not in argv


def test_a_missing_notify_send_does_not_raise(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("notify-send")

    monkeypatch.setattr(core_notify.subprocess, "run", boom)
    core_notify.notify("no notification daemon here")
