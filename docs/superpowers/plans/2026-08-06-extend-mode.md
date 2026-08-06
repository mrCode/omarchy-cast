# Extend Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cast be either a mirror of the laptop screen or an extend — a Hyprland virtual output that becomes a second monitor on the receiver.

**Architecture:** A new `core/virtual_display.py` owns all `hyprctl output` calls, mirroring how `core/display.py` owns mode switching. `backends/creds.py` decides which credentials file doubletake is given, so each mode keeps its own portal restore token. `mode` travels on the start request, is stored on the `Session`, and is reported by `status`.

**Tech Stack:** Python 3.11+, asyncio, Hyprland `hyprctl`, doubletake `-creds`, pytest.

## Global Constraints

- Virtual output name: `omarchy-cast`. Configuration string: `<name>,1920x1080@60,auto,1`.
- `create()` must return the name it **observes** after creating, not the name it requested. Assuming the name left a stray `HEADLESS-2` during design testing.
- No test may invoke `hyprctl`, create an output, or touch the real display. Backend tests already default `airplay_auto_resolution=False` for this reason; extend tests must do the equivalent.
- Extend must **not** switch the laptop display. That workaround exists only because doubletake negotiates 1080p; a virtual output is already 1080p.
- `mode` defaults to `"mirror"` everywhere, so existing behaviour and every existing test is unchanged.
- Chromecast extend ships but is never described as working; the Cast backend has never run against hardware.
- Existing signature, unchanged for mirror: `AirPlayBackend(on_state, config, spawner=None, ready_timeout=READY_TIMEOUT)`.

---

### Task 1: Virtual display module

**Files:**
- Create: `omarchy_cast/core/virtual_display.py`
- Test: `tests/test_virtual_display.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `VIRTUAL_NAME = "omarchy-cast"`
  - `available() -> bool`
  - `create(runner=_run) -> str | None` — returns the observed output name, or None on failure
  - `remove(name: str, runner=_run) -> bool`
  - `cleanup_strays(runner=_run) -> int`
  - `_run(argv: list[str]) -> tuple[int, str]` — default runner, injectable in tests

- [ ] **Step 1: Write the failing test**

`tests/test_virtual_display.py`:

```python
import json

import pytest

from omarchy_cast.core import virtual_display
from omarchy_cast.core.virtual_display import (
    VIRTUAL_NAME,
    cleanup_strays,
    create,
    remove,
)


@pytest.fixture(autouse=True)
def hyprctl_present(monkeypatch):
    monkeypatch.setattr(virtual_display, "available", lambda: True)


def monitors(*names):
    return json.dumps([
        {"name": n, "width": 1920, "height": 1080, "refreshRate": 60.0,
         "x": 0, "y": 0, "scale": 1.0}
        for n in names
    ])


class FakeRunner:
    """Simulates hyprctl: `output create` adds a monitor, `remove` deletes it."""

    def __init__(self, existing=("eDP-2",), created_as=None, fail_on=None):
        self.names = list(existing)
        self._created_as = created_as
        self._fail_on = fail_on
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        joined = " ".join(argv)
        if self._fail_on and self._fail_on in joined:
            return 1, ""
        if "monitors" in argv:
            return 0, monitors(*self.names)
        if "create" in argv:
            # Hyprland may ignore the requested name and use HEADLESS-N.
            self.names.append(self._created_as or VIRTUAL_NAME)
            return 0, "ok"
        if "remove" in argv:
            target = argv[-1]
            if target in self.names:
                self.names.remove(target)
            return 0, "ok"
        return 0, "ok"


def test_create_returns_the_new_output_name():
    runner = FakeRunner()
    assert create(runner) == VIRTUAL_NAME
    assert VIRTUAL_NAME in runner.names


def test_create_returns_the_observed_name_not_the_requested_one():
    """Design testing left a stray HEADLESS-2 by trusting the requested name."""
    runner = FakeRunner(created_as="HEADLESS-7")
    assert create(runner) == "HEADLESS-7"


def test_create_configures_1080p_at_scale_1():
    """Default scale is 2.0, giving a useless logical 960x540."""
    runner = FakeRunner()
    create(runner)
    keyword = [c for c in runner.calls if "keyword" in c][-1]
    assert "1920x1080@60" in keyword[-1]
    assert keyword[-1].endswith(",auto,1")
    assert keyword[-1].startswith(f"{VIRTUAL_NAME},")


def test_create_returns_none_when_creation_fails():
    runner = FakeRunner(fail_on="output create")
    assert create(runner) is None


def test_create_returns_none_when_no_new_monitor_appears():
    class Silent(FakeRunner):
        def __call__(self, argv):
            self.calls.append(argv)
            if "monitors" in argv:
                return 0, monitors(*self.names)
            return 0, "ok"

    assert create(Silent()) is None


def test_remove_deletes_the_output():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME))
    assert remove(VIRTUAL_NAME, runner) is True
    assert VIRTUAL_NAME not in runner.names


def test_remove_reports_failure():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME), fail_on="output remove")
    assert remove(VIRTUAL_NAME, runner) is False


def test_cleanup_removes_strays_including_headless():
    runner = FakeRunner(existing=("eDP-2", VIRTUAL_NAME, "HEADLESS-2"))
    assert cleanup_strays(runner) == 2
    assert runner.names == ["eDP-2"]


def test_cleanup_leaves_real_monitors_alone():
    runner = FakeRunner(existing=("eDP-2", "HDMI-A-1"))
    assert cleanup_strays(runner) == 0
    assert runner.names == ["eDP-2", "HDMI-A-1"]


def test_nothing_happens_without_hyprctl(monkeypatch):
    monkeypatch.setattr(virtual_display, "available", lambda: False)
    runner = FakeRunner()
    assert create(runner) is None
    assert not runner.calls


def test_unparseable_monitor_output_is_handled():
    class Garbage(FakeRunner):
        def __call__(self, argv):
            self.calls.append(argv)
            return 0, "not json"

    assert create(Garbage()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_virtual_display.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.core.virtual_display'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/core/virtual_display.py`:

```python
"""Hyprland virtual outputs, used for extend mode.

The only module that runs `hyprctl output`. Backends go through it, the same
way `core/display.py` isolates display-mode switching, so the whole test suite
can run without a compositor.
"""

import json
import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

VIRTUAL_NAME = "omarchy-cast"
MODE_LINE = "1920x1080@60"

# A virtual output defaults to scale 2.0 -- a logical 960x540, useless as a
# desktop. `auto` places it to the right of existing outputs.
CONFIG = "{name}," + MODE_LINE + ",auto,1"


def _run(argv: list[str]) -> tuple[int, str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout


def available() -> bool:
    return shutil.which("hyprctl") is not None


def _monitor_names(runner) -> set[str] | None:
    code, out = runner(["hyprctl", "-j", "monitors"])
    if code != 0:
        return None
    try:
        return {m["name"] for m in json.loads(out)}
    except (json.JSONDecodeError, TypeError, KeyError):
        log.debug("could not parse hyprctl monitors output")
        return None


def _is_virtual(name: str) -> bool:
    return name == VIRTUAL_NAME or name.startswith("HEADLESS")


def create(runner=_run) -> str | None:
    """Create the virtual output and return the name Hyprland actually used."""
    if not available():
        log.debug("hyprctl unavailable; cannot create a virtual output")
        return None

    before = _monitor_names(runner)
    if before is None:
        return None

    code, _ = runner(["hyprctl", "output", "create", "headless", VIRTUAL_NAME])
    if code != 0:
        log.warning("could not create a virtual output")
        return None

    after = _monitor_names(runner)
    if after is None:
        return None

    new = sorted(after - before)
    if not new:
        log.warning("hyprctl reported success but no new output appeared")
        return None

    name = new[0]
    if name != VIRTUAL_NAME:
        # Naming is undocumented; if a Hyprland version drops it the name
        # changes every run and the portal restore token breaks each time.
        log.warning(
            "requested output name %r but got %r; the portal will re-prompt "
            "on every cast", VIRTUAL_NAME, name,
        )

    # Checked: a failed geometry call leaves the output at Hyprland's default
    # scale 2.0 -- a logical 960x540 -- and returning success there would hand
    # the caller a quarter-size display with no signal anything went wrong.
    code, _ = runner(["hyprctl", "keyword", "monitor", CONFIG.format(name=name)])
    if code != 0:
        log.warning("could not configure %s; removing the half-created output", name)
        remove(name, runner)
        return None

    log.info("created virtual output %s at %s", name, MODE_LINE)
    return name


def remove(name: str, runner=_run) -> bool:
    if not available():
        return False
    code, _ = runner(["hyprctl", "output", "remove", name])
    if code != 0:
        log.warning("could not remove virtual output %s", name)
        return False
    log.info("removed virtual output %s", name)
    return True


def cleanup_strays(runner=_run) -> int:
    """Remove virtual outputs left behind by a crash. Called at daemon start."""
    if not available():
        return 0
    names = _monitor_names(runner)
    if not names:
        return 0
    removed = 0
    for name in sorted(names):
        if _is_virtual(name) and remove(name, runner):
            removed += 1
    return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_virtual_display.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/core/virtual_display.py tests/test_virtual_display.py
git commit -m "feat: add Hyprland virtual output management"
```

---

### Task 2: Per-mode credentials

**Files:**
- Create: `omarchy_cast/backends/creds.py`
- Test: `tests/test_creds.py`

**Interfaces:**
- Consumes: `state_dir()` from `omarchy_cast.core.display`.
- Produces:
  - `MIRROR = "mirror"`, `EXTEND = "extend"`, `MODES = (MIRROR, EXTEND)`
  - `default_creds_path() -> Path` — doubletake's own file
  - `extend_creds_path() -> Path`
  - `ensure_extend_creds() -> Path` — copies the mirror file minus `restore_token`
  - `creds_path(mode: str) -> Path | None` — None means "use doubletake's default"

- [ ] **Step 1: Write the failing test**

`tests/test_creds.py`:

```python
import json

import pytest

from omarchy_cast.backends import creds
from omarchy_cast.backends.creds import (
    EXTEND,
    MIRROR,
    creds_path,
    ensure_extend_creds,
    extend_creds_path,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(creds, "default_creds_path", lambda: tmp_path / "doubletake.json")
    return tmp_path


def write_mirror_creds(tmp_path, with_token=True):
    data = {
        "AA:BB:CC:DD:EE:01": {
            "pairing_id": "abc",
            "ed25519_public": "pub",
            "ed25519_seed": "seed",
        }
    }
    if with_token:
        data["AA:BB:CC:DD:EE:01"]["restore_token"] = "tok-mirror"
    p = tmp_path / "doubletake.json"
    p.write_text(json.dumps(data))
    return p


def test_mirror_uses_doubletake_default():
    assert creds_path(MIRROR) is None


def test_extend_uses_its_own_file(isolated):
    write_mirror_creds(isolated)
    assert creds_path(EXTEND) == extend_creds_path()


def test_extend_creds_copy_the_pairing(isolated):
    write_mirror_creds(isolated)
    path = ensure_extend_creds()
    data = json.loads(path.read_text())
    assert data["AA:BB:CC:DD:EE:01"]["pairing_id"] == "abc"
    assert data["AA:BB:CC:DD:EE:01"]["ed25519_seed"] == "seed"


def test_extend_creds_drop_the_restore_token(isolated):
    """Copying it would restore the mirror's output and silently mirror."""
    write_mirror_creds(isolated)
    data = json.loads(ensure_extend_creds().read_text())
    assert "restore_token" not in data["AA:BB:CC:DD:EE:01"]


def test_existing_extend_creds_are_not_overwritten(isolated):
    """Otherwise every cast would discard the stored output selection."""
    write_mirror_creds(isolated)
    path = ensure_extend_creds()
    data = json.loads(path.read_text())
    data["AA:BB:CC:DD:EE:01"]["restore_token"] = "tok-extend"
    path.write_text(json.dumps(data))

    ensure_extend_creds()
    again = json.loads(path.read_text())
    assert again["AA:BB:CC:DD:EE:01"]["restore_token"] == "tok-extend"


def test_missing_mirror_creds_still_yields_a_usable_path(isolated):
    """First ever cast is an extend: there is nothing to copy, and pairing
    will simply happen in the extend file instead."""
    path = ensure_extend_creds()
    assert path == extend_creds_path()
    assert json.loads(path.read_text()) == {}


def test_corrupt_mirror_creds_do_not_propagate(isolated):
    (isolated / "doubletake.json").write_text("{not json")
    path = ensure_extend_creds()
    assert json.loads(path.read_text()) == {}


def test_extend_creds_are_private(isolated):
    write_mirror_creds(isolated)
    assert ensure_extend_creds().stat().st_mode & 0o077 == 0


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown mode"):
        creds_path("sideways")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_creds.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.backends.creds'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/backends/creds.py`:

```python
"""Which credentials file doubletake gets, per cast mode.

doubletake stores one `restore_token` per device, which is its portal output
selection. Mirror and extend need different outputs, so they get different
files via doubletake's `-creds` flag. That avoids editing doubletake's own
store or depending on its JSON layout beyond removing one key from our copy.
"""

import json
import logging
from pathlib import Path

from omarchy_cast.core.display import state_dir

log = logging.getLogger(__name__)

MIRROR = "mirror"
EXTEND = "extend"
MODES = (MIRROR, EXTEND)


def default_creds_path() -> Path:
    return Path.home() / ".config" / "doubletake" / "credentials.json"


def extend_creds_path() -> Path:
    return state_dir() / "doubletake-extend-credentials.json"


def ensure_extend_creds() -> Path:
    """Create the extend credentials file if absent, and return its path.

    The pairing is copied so extend does not need a second PIN. The restore
    token is dropped: keeping it would restore the mirror's output selection
    and silently mirror instead of extending.
    """
    path = extend_creds_path()
    if path.exists():
        return path

    data: dict = {}
    source = default_creds_path()
    if source.exists():
        try:
            data = json.loads(source.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("could not read mirror credentials (%s); starting fresh", exc)
            data = {}

    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict):
                entry.pop("restore_token", None)
    else:
        data = {}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    log.info("created extend credentials at %s", path)
    return path


def creds_path(mode: str) -> Path | None:
    """None means: let doubletake use its own default file."""
    if mode == MIRROR:
        return None
    if mode == EXTEND:
        return ensure_extend_creds()
    raise ValueError(f"unknown mode: {mode}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_creds.py -q`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/backends/creds.py tests/test_creds.py
git commit -m "feat: give each cast mode its own doubletake credentials"
```

---

### Task 3: Mode plumbing through session, daemon and CLI

**Files:**
- Modify: `omarchy_cast/core/session.py`
- Modify: `omarchy_cast/core/daemon.py`
- Modify: `omarchy_cast/backends/base.py`
- Modify: `omarchy_cast/backends/stub.py`
- Modify: `omarchy_cast/cli/main.py`
- Test: `tests/test_mode.py`

**Interfaces:**
- Consumes: `MIRROR`, `EXTEND`, `MODES` from `backends/creds.py`.
- Produces:
  - `Session(device, mode: str = "mirror")` with `.mode`
  - `Backend.start(self, device: Device, mode: str = MIRROR) -> None`
  - Daemon `start` accepts `mode`, rejects unknown values, reports `mode` in `status`
  - CLI `--mode {mirror,extend}` on `start`

- [ ] **Step 1: Write the failing test**

`tests/test_mode.py`:

```python
import pytest

from omarchy_cast.backends.creds import EXTEND, MIRROR
from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core.daemon import Daemon
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import Session


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


def make_device(protocol="cast", ident="1"):
    return Device(
        id=Device.make_id(protocol, ident), name=f"{protocol}-{ident}",
        address="192.168.1.5", port=8009, protocol=protocol,
    )


def make_daemon():
    daemon = Daemon(FakeDiscovery([make_device()]), {}, notifier=lambda m: None)
    daemon.backends["cast"] = StubBackend(daemon.on_state)
    return daemon


def test_session_defaults_to_mirror():
    assert Session(make_device()).mode == MIRROR


def test_session_records_its_mode():
    assert Session(make_device(), mode=EXTEND).mode == EXTEND


async def test_start_defaults_to_mirror():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions["cast:1"].mode == MIRROR


async def test_start_accepts_extend():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1", "mode": "extend"})
    assert daemon.sessions["cast:1"].mode == EXTEND


async def test_start_rejects_an_unknown_mode():
    daemon = make_daemon()
    resp = await daemon.handle(
        {"cmd": "start", "device_id": "cast:1", "mode": "sideways"}
    )
    assert resp["ok"] is False
    assert "sideways" in resp["error"]


async def test_status_reports_the_mode():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1", "mode": "extend"})
    resp = await daemon.handle({"cmd": "status"})
    assert resp["data"]["sessions"][0]["mode"] == EXTEND


async def test_backend_receives_the_mode():
    seen = {}

    class Recording(StubBackend):
        async def start(self, device, mode=MIRROR):
            seen["mode"] = mode
            await super().start(device)

    daemon = make_daemon()
    daemon.backends["cast"] = Recording(daemon.on_state)
    await daemon.handle({"cmd": "start", "device_id": "cast:1", "mode": "extend"})
    assert seen["mode"] == EXTEND


def test_cli_passes_the_mode(monkeypatch):
    from omarchy_cast.cli import main as cli_main

    calls = []

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
        return {"ok": True, "data": {"state": "streaming"}}

    monkeypatch.setattr(cli_main, "request", _request)
    assert cli_main.main(["start", "cast:1", "--mode", "extend"]) == 0
    assert calls[0][1]["mode"] == "extend"


def test_cli_defaults_to_mirror(monkeypatch):
    from omarchy_cast.cli import main as cli_main

    calls = []

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
        return {"ok": True, "data": {"state": "streaming"}}

    monkeypatch.setattr(cli_main, "request", _request)
    cli_main.main(["start", "cast:1"])
    assert calls[0][1]["mode"] == "mirror"


def test_cli_rejects_a_bad_mode(monkeypatch):
    from omarchy_cast.cli import main as cli_main

    with pytest.raises(SystemExit):
        cli_main.main(["start", "cast:1", "--mode", "sideways"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mode.py -q`
Expected: FAIL with `TypeError: Session.__init__() got an unexpected keyword argument 'mode'`

- [ ] **Step 3: Add mode to Session**

In `omarchy_cast/core/session.py`, replace the `Session.__init__` body:

```python
    def __init__(self, device: Device, mode: str = "mirror") -> None:
        self.device = device
        self.mode = mode
        self.state = SessionState.IDLE
        self.error: str | None = None
        self.started_at: float | None = None
```

- [ ] **Step 4: Add mode to the Backend interface**

In `omarchy_cast/backends/base.py`, change the abstract signature:

```python
    @abstractmethod
    async def start(self, device: Device, mode: str = "mirror") -> None: ...
```

In `omarchy_cast/backends/stub.py`, change `StubBackend.start` to match:

```python
    async def start(self, device: Device, mode: str = "mirror") -> None:
```

(The body is unchanged — the stub ignores the mode.)

- [ ] **Step 5: Thread mode through the daemon**

In `omarchy_cast/core/daemon.py`, add the import near the other backend imports:

```python
from omarchy_cast.backends.creds import MODES
```

Replace `_cmd_start` with:

```python
    async def _cmd_start(self, request: dict) -> dict:
        device_id = request.get("device_id")
        mode = request.get("mode", "mirror")
        if mode not in MODES:
            return err(f"unknown mode: {mode}; expected one of {MODES}")

        device = self._find_device(device_id)
        if device is None:
            return err(f"device not found: {device_id}")

        backend = self.backends.get(device.protocol)
        if backend is None:
            return err(f"no backend for protocol: {device.protocol}")

        self._pending_mode = mode
        try:
            await backend.start(device, mode)
        finally:
            self._pending_mode = "mirror"

        session = self.sessions.get(device.id)
        data = {"state": str(session.state) if session else "idle", "mode": mode}
        if device.protocol == "cast":
            data["warning"] = CAST_UNTESTED
            log.warning(CAST_UNTESTED)
        return ok(data)
```

In `Daemon.__init__`, add before `self._last_active`:

```python
        # Set for the duration of a start so on_state can label the session.
        self._pending_mode = "mirror"
```

In `on_state`, change the session creation line:

```python
        if session is None:
            session = Session(device, mode=self._pending_mode)
            self.sessions[device.id] = session
```

In `_cmd_status`, add `mode` to each session dict:

```python
                "state": str(s.state),
                "mode": s.mode,
                "error": s.error,
```

- [ ] **Step 6: Add the CLI flag**

In `omarchy_cast/cli/main.py`, add to the `start` subparser:

```python
    start.add_argument(
        "--mode",
        default="mirror",
        choices=("mirror", "extend"),
        help="mirror the screen (default) or extend onto a virtual display",
    )
```

and change the start branch in `main()` to pass it:

```python
            response = asyncio.run(request("start", device_id=device_id, mode=args.mode))
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest -q`
Expected: PASS — the new file's 10 tests plus every existing test, since `mode` defaults to `"mirror"` throughout.

- [ ] **Step 8: Commit**

```bash
git add omarchy_cast tests/test_mode.py
git commit -m "feat: carry a cast mode through session, daemon and CLI"
```

---

### Task 4: Extend in the AirPlay backend

**Files:**
- Modify: `omarchy_cast/backends/airplay.py`
- Test: `tests/test_airplay_extend.py`

**Interfaces:**
- Consumes: `create`, `remove`, `cleanup_strays`, `VIRTUAL_NAME` from `core/virtual_display.py`; `creds_path` from `backends/creds.py`.
- Produces: `AirPlayBackend.start(device, mode=MIRROR)` honouring both modes; `build_argv(device, mode=MIRROR)` gaining `-creds` for extend.

- [ ] **Step 1: Write the failing test**

`tests/test_airplay_extend.py`:

```python
import pytest

from omarchy_cast.backends import airplay as airplay_mod
from omarchy_cast.backends.airplay import AirPlayBackend
from omarchy_cast.backends.creds import EXTEND, MIRROR
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState

from tests.test_airplay_backend import READY, FakeProc


def make_device():
    return Device(
        id="airplay:AA", name="Living Room", address="192.168.1.77",
        port=7000, protocol="airplay",
    )


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """No hyprctl, no display changes, no real credentials."""
    events = []
    monkeypatch.setattr(airplay_mod.virtual_display, "cleanup_strays",
                        lambda *a, **k: events.append("cleanup") or 0)
    monkeypatch.setattr(airplay_mod.virtual_display, "create",
                        lambda *a, **k: events.append("create") or "omarchy-cast")
    monkeypatch.setattr(airplay_mod.virtual_display, "remove",
                        lambda name, *a, **k: events.append(f"remove:{name}") or True)
    monkeypatch.setattr(airplay_mod.display, "apply_stream_mode",
                        lambda *a, **k: events.append("switch-display"))
    monkeypatch.setattr(airplay_mod.display, "restore_mode",
                        lambda *a, **k: events.append("restore-display") or True)
    monkeypatch.setattr(airplay_mod, "creds_path",
                        lambda mode: (tmp_path / "extend.json") if mode == EXTEND else None)
    return events


def make_backend(proc, **cfg):
    cfg.setdefault("airplay_auto_resolution", True)
    states = []
    spawned = {}

    async def spawner(argv, env):
        spawned["argv"] = argv
        return proc

    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(**cfg), spawner=spawner, ready_timeout=1.0,
    )
    return backend, states, spawned


async def test_extend_creates_a_virtual_output(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    assert "create" in fakes
    assert states[-1][0] is SessionState.STREAMING
    await backend.shutdown()


async def test_extend_does_not_touch_the_display(fakes):
    """The virtual output is already 1080p; switching would be gratuitous."""
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    assert "switch-display" not in fakes
    await backend.shutdown()


async def test_mirror_still_switches_the_display(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), MIRROR)
    assert "switch-display" in fakes
    assert "create" not in fakes
    await backend.shutdown()


async def test_extend_passes_its_own_creds_file(fakes, tmp_path):
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    argv = spawned["argv"]
    assert "-creds" in argv
    assert argv[argv.index("-creds") + 1] == str(tmp_path / "extend.json")
    await backend.shutdown()


async def test_mirror_passes_no_creds_flag(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, spawned = make_backend(proc)
    await backend.start(make_device(), MIRROR)
    assert "-creds" not in spawned["argv"]
    await backend.shutdown()


async def test_stop_removes_the_virtual_output(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    device = make_device()
    await backend.start(device, EXTEND)
    await backend.stop(device)
    assert "remove:omarchy-cast" in fakes


async def test_failed_start_removes_the_virtual_output(fakes):
    """A failure must not leave a phantom monitor on the desktop."""
    proc = FakeProc([b"mirror setup failed\n"], exit_on_eof=True)
    backend, _, _ = make_backend(proc)
    with pytest.raises(Exception):
        await backend.start(make_device(), EXTEND)
    assert any(e.startswith("remove:") for e in fakes)


async def test_shutdown_removes_the_virtual_output(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    await backend.shutdown()
    assert any(e.startswith("remove:") for e in fakes)


async def test_strays_are_cleaned_before_creating(fakes):
    proc = FakeProc([READY + b"\n"])
    backend, _, _ = make_backend(proc)
    await backend.start(make_device(), EXTEND)
    assert fakes.index("cleanup") < fakes.index("create")
    await backend.shutdown()


async def test_extend_fails_clearly_when_the_output_cannot_be_created(
    fakes, monkeypatch
):
    monkeypatch.setattr(airplay_mod.virtual_display, "create", lambda *a, **k: None)
    proc = FakeProc([READY + b"\n"])
    backend, states, _ = make_backend(proc)
    with pytest.raises(Exception, match="virtual display"):
        await backend.start(make_device(), EXTEND)
    assert states[-1][0] is SessionState.FAILED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_airplay_extend.py -q`
Expected: FAIL with `AttributeError: module 'omarchy_cast.backends.airplay' has no attribute 'virtual_display'`

- [ ] **Step 3: Write minimal implementation**

In `omarchy_cast/backends/airplay.py`, add to the imports:

```python
from omarchy_cast.backends.creds import EXTEND, MIRROR, creds_path
from omarchy_cast.core import display, virtual_display
```

(The `display` import already exists — extend the existing line rather than duplicating it.)

Add to `AirPlayBackend.__init__`, after `self._sessions`:

```python
        # Name of the virtual output while an extend session is running.
        self._virtual: str | None = None
```

Replace `build_argv` with a mode-aware version:

```python
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
```

Replace the opening of `start` (down to the spawn) with:

```python
    async def start(self, device: Device, mode: str = MIRROR) -> None:
        await self._teardown(device.id)
        self._emit(device, SessionState.CONNECTING)

        if mode == EXTEND:
            virtual_display.cleanup_strays()
            name = virtual_display.create()
            if name is None:
                message = (
                    "could not create a virtual display for extend mode; "
                    "is this Hyprland with hyprctl available?"
                )
                self._emit(device, SessionState.FAILED, message)
                raise BackendError(message)
            self._virtual = name
        elif self._config.airplay_auto_resolution:
            # Mirror only: the receiver rejects a stream whose SPS does not
            # match the negotiated 1920x1080. A virtual output is already 1080p.
            display.apply_stream_mode()

        try:
            proc = await self._spawn(self.build_argv(device, mode), self.daemon_env())
        except FileNotFoundError as exc:
            message = f"{BIN} is not installed; install it with: yay -S doubletake"
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message) from exc
```

Replace `_restore_display` with a method that undoes whichever mode was used:

```python
    def _restore_environment(self) -> None:
        if self._sessions:
            return
        if self._virtual is not None:
            virtual_display.remove(self._virtual)
            self._virtual = None
        elif self._config.airplay_auto_resolution:
            display.restore_mode()
```

Then replace every call to `self._restore_display()` with
`self._restore_environment()` — there are three: in `_teardown`'s early return,
at the end of `_teardown`, and in `_pump`'s crash branch.

Note that `start` calls `_teardown` first, which clears `self._virtual`, so a
failed extend still removes the output it created.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest -q`
Expected: PASS — the 10 new tests plus all existing ones.

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/backends/airplay.py tests/test_airplay_extend.py
git commit -m "feat: extend mode casts a virtual display instead of the screen"
```

---

### Task 5: Daemon cleanup, menu, TUI and waybar

**Files:**
- Modify: `omarchy_cast/core/daemon.py`
- Modify: `omarchy_cast/cli/menu.py`
- Modify: `omarchy_cast/cli/main.py`
- Modify: `omarchy_cast/cli/waybar.py`
- Modify: `omarchy_cast/tui/app.py`
- Test: `tests/test_mode_ui.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `menu.MODE_ENTRIES: tuple[str, str]` and `menu.parse_mode(line) -> str | None`
  - waybar tooltip includes the mode while casting
  - TUI binding `e` starts the highlighted device in extend mode

- [ ] **Step 1: Write the failing test**

`tests/test_mode_ui.py`:

```python
from omarchy_cast.cli.menu import MODE_ENTRIES, parse_mode
from omarchy_cast.cli.waybar import render


def test_mode_entries_offer_both():
    assert len(MODE_ENTRIES) == 2
    assert any("Mirror" in e for e in MODE_ENTRIES)
    assert any("Extend" in e for e in MODE_ENTRIES)


def test_parse_mode_round_trips():
    modes = {parse_mode(e) for e in MODE_ENTRIES}
    assert modes == {"mirror", "extend"}


def test_parse_mode_rejects_noise():
    assert parse_mode("") is None
    assert parse_mode("something else") is None


def test_extend_entry_names_the_output_to_pick():
    """Choosing the wrong output at the portal prompt silently mirrors."""
    extend = next(e for e in MODE_ENTRIES if "Extend" in e)
    assert "omarchy-cast" in extend


def test_waybar_tooltip_shows_the_mode():
    out = render([{
        "name": "Living Room", "protocol": "airplay",
        "state": "streaming", "mode": "extend", "error": None,
    }])
    assert "extend" in out["tooltip"].lower()


def test_waybar_tooltip_without_a_mode_still_works():
    """Older daemons omit the field; the indicator must not crash."""
    out = render([{
        "name": "Living Room", "protocol": "airplay",
        "state": "streaming", "error": None,
    }])
    assert "Living Room" in out["tooltip"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mode_ui.py -q`
Expected: FAIL with `ImportError: cannot import name 'MODE_ENTRIES'`

- [ ] **Step 3: Add the mode prompt to the menu**

In `omarchy_cast/cli/menu.py`, add after `STOP_ENTRY`:

```python
# Shown as a second walker prompt after a device is chosen. The extend entry
# names the output because picking the wrong one at the portal prompt silently
# produces a mirror that then repeats on every cast.
MODE_ENTRIES = (
    "Mirror — show this screen on the receiver",
    "Extend — second display (pick 'omarchy-cast' if the portal asks)",
)


def parse_mode(line: str) -> str | None:
    line = line.strip()
    if line.startswith("Mirror"):
        return "mirror"
    if line.startswith("Extend"):
        return "extend"
    return None
```

- [ ] **Step 4: Wire the prompt into the menu flow**

In `omarchy_cast/cli/main.py`, update the import:

```python
from omarchy_cast.cli.menu import (
    MANUAL_ENTRY,
    MODE_ENTRIES,
    STOP_ENTRY,
    format_entries,
    parse_mode,
    parse_selection,
)
```

In `_run_menu`, replace the final start block with:

```python
    mode = parse_mode(_walker(list(MODE_ENTRIES), "Mirror or extend?"))
    if mode is None:
        return 0

    result = asyncio.run(request("start", device_id=device_id, mode=mode))
    if not result.get("ok"):
        message = result.get("error", "unknown error")
        _notify(message, urgent=True)
        return _fail(message)
    warning = (result.get("data") or {}).get("warning")
    if warning:
        _notify(warning)
    elif mode == "extend":
        _notify(
            "Extending — if the portal asks, share the 'omarchy-cast' output. "
            "Right-click the waybar icon to stop."
        )
    else:
        _notify("Casting started — right-click the waybar icon to stop")
    return 0
```

- [ ] **Step 5: Show the mode in waybar**

In `omarchy_cast/cli/waybar.py`, replace the final return of `render`:

```python
    names = ", ".join(s["name"] for s in sessions)
    text = ICON_ACTIVE if len(sessions) == 1 else f"{ICON_ACTIVE} {len(sessions)}"
    modes = {s.get("mode") for s in sessions if s.get("mode")}
    label = f" ({'/'.join(sorted(modes))})" if modes else ""
    return {
        "text": text,
        "tooltip": f"Casting to {names}{label}\n{HINT_ACTIVE}",
        "class": "streaming",
    }
```

- [ ] **Step 6: Add the TUI binding**

In `omarchy_cast/tui/app.py`, add to `BINDINGS` after the `enter` entry:

```python
        Binding("e", "extend", "Extend"),
```

and add these methods next to `action_start`:

```python
    def action_extend(self) -> None:
        self._start_in_mode("extend")

    def action_start(self) -> None:
        """Synchronous on purpose.

        The guard must be set before any await, otherwise several rapid Enter
        presses all dispatch workers before the first marks us busy -- which is
        exactly how five stacked casts and a looping display happened.
        """
        self._start_in_mode("mirror")

    def _start_in_mode(self, mode: str) -> None:
        if self._busy:
            return
        row = self._selected()
        if row is None:
            return
        if row.get("state") in ("connecting", "awaiting_pin", "streaming"):
            self._set_summary(f"{row['name']} is already {row['state']}", "connecting")
            return

        self._busy = True
        self._set_summary(
            f"{mode}ing to {row['name']} (this takes a few seconds)...", "connecting"
        )
        self._start_worker(row["id"], mode)
```

and change the worker to take the mode:

```python
    @work(exclusive=True, group="control")
    async def _start_worker(self, device_id: str, mode: str = "mirror") -> None:
        try:
            await self._send("start", device_id=device_id, mode=mode)
        finally:
            self._busy = False
```

- [ ] **Step 7: Clean up strays at daemon start**

In `omarchy_cast/core/daemon.py`, inside `main()`, extend the existing recovery block:

```python
    # A previous run may have died mid-cast with the display still switched.
    from omarchy_cast.core import display, virtual_display
    if display.restore_mode():
        log.info("restored a display mode left over from a previous session")
    strays = virtual_display.cleanup_strays()
    if strays:
        log.info("removed %d virtual output(s) left over from a previous session", strays)
```

- [ ] **Step 8: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: PASS, all tests green.

- [ ] **Step 9: Commit**

```bash
git add omarchy_cast tests/test_mode_ui.py
git commit -m "feat: choose mirror or extend from the menu, TUI and waybar"
```

---

### Task 6: Documentation and hardware verification

**Files:**
- Modify: `README.md`
- Modify: `docs/device-matrix.md`
- Modify: `pyproject.toml`, `PKGBUILD` (version bump)

- [ ] **Step 1: Document extend in the README**

Add a section after the TUI section covering:

- What extend does: a virtual 1920×1080 output appears in Hyprland; drag windows onto it; it disappears when the cast stops.
- That extend does **not** change your display resolution, unlike mirror.
- The first-run portal prompt: **choose the `omarchy-cast` output**, and that picking the wrong one silently mirrors.
- Recovery from a wrong pick: `rm ~/.local/state/omarchy-cast/doubletake-extend-credentials.json`, which forces a fresh prompt.
- `omarchy-cast start <id> --mode extend`, and `e` in the TUI.

- [ ] **Step 2: Bump the version**

Set `version = "0.2.0"` in `pyproject.toml` and `pkgver=0.2.0` in `PKGBUILD`. This is a feature release, not a fix.

- [ ] **Step 3: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS. Record the count.

- [ ] **Step 4: Verify against real hardware**

These cannot be automated — both modes look identical from the process table.

1. `omarchy-cast start <id> --mode extend` — portal prompts once; choose `omarchy-cast`.
2. Confirm `hyprctl monitors` lists `omarchy-cast` at 1920x1080 scale 1.
3. **Confirm the receiver shows an empty desktop, not a copy of the laptop.** This is the whole feature.
4. Confirm the laptop resolution is unchanged (`hyprctl monitors` still shows the panel's native mode).
5. Drag a window onto the virtual output; confirm it appears on the receiver.
6. `omarchy-cast stop` — the output disappears and any windows return to the laptop.
7. Start extend a second time; confirm **no** portal prompt (the restore token held).
8. Kill the daemon mid-extend with SIGTERM; confirm no leftover output and no stray processes.
9. Repeat step 1 in mirror mode; confirm it still switches the display and restores it.

If any step fails, fix it before marking this task complete.

- [ ] **Step 5: Record the result**

Update `docs/device-matrix.md` with an extend row for the tested receiver, including whether the portal prompt recurred. A confirmed failure is as valuable as a success.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/device-matrix.md pyproject.toml PKGBUILD
git commit -m "docs: document extend mode and record hardware results"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: `virtual_display.py` → Task 1; `creds.py` → Task 2; mode on session/daemon/CLI → Task 3; AirPlay extend, display-switch suppression, and all teardown paths → Task 4; menu/TUI/waybar and daemon stray cleanup → Task 5; documentation, the wrong-output recovery instructions, and manual verification → Task 6.

**One spec item deliberately deferred:** `CastBackend` does not receive extend support. The spec allows shipping it untested, but `CastBackend.start` would need the same mode parameter and its own portal-token-per-mode handling, and the backend has never run against hardware — so adding an unverifiable second path here would be guesswork on top of guesswork. Its `start` keeps the default `mode="mirror"` parameter from the ABC change in Task 3, so it stays interface-compatible and can be done later. This narrows the spec's stated scope and should be flagged to the user.

**Placeholder scan.** No TBD/TODO markers. Every code step carries real code. Task 6 Step 1 describes README content rather than quoting it, because it is prose whose wording should follow the existing README's voice — the required facts are enumerated.

**Type consistency.** `mode` is a plain `str` everywhere, with `MIRROR`/`EXTEND`/`MODES` from `backends/creds.py` as the single source of truth. `Session(device, mode=...)`, `Backend.start(device, mode=...)`, `build_argv(device, mode=...)`, `creds_path(mode)`, `parse_mode()` and the daemon's `mode` field all agree. `virtual_display.create()` returns `str | None`; the backend stores it as `self._virtual: str | None`. `_restore_display` is renamed to `_restore_environment` in Task 4, and the plan names all three call sites that must change.
