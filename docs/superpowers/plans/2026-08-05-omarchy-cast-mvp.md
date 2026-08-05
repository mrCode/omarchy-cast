# omarchy-cast MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working `omarchy-cast` that mirrors the Hyprland desktop to an Apple TV or a Chromecast, driven from a walker menu with a waybar indicator.

**Architecture:** An asyncio daemon (`omarchy-castd`) owns mDNS discovery, session state, and the Cast media pipeline. Thin clients talk to it over a JSON-lines Unix socket. AirPlay is delegated to the external `doubletake` binary via its `doubletake-ctl` CLI; Cast is implemented here with pychromecast for the CastV2 channel and a GStreamer pipeline whose `appsink` output is streamed over a hand-rolled HTTP server to the Default Media Receiver.

**Tech Stack:** Python 3.11+, asyncio, pychromecast, python-zeroconf, PyGObject (Gio D-Bus + GStreamer), pytest.

## Global Constraints

- Python 3.11+ (`tomllib` is stdlib from 3.11). Target system runs 3.14.6.
- Runtime dependencies limited to official Arch `extra` packages: `python-pychromecast`, `python-zeroconf`, `python-gobject`, `gst-python`, `gst-plugin-va`, `gst-plugins-bad`, `gst-plugins-good`. The only AUR runtime dependency is `doubletake`.
- No new third-party HTTP or D-Bus libraries. Use `Gio.DBusProxy` for the portal and asyncio streams for HTTP.
- Control socket path: `$XDG_RUNTIME_DIR/omarchy-cast.sock`
- Config path: `~/.config/omarchy-cast/config.toml`
- State path: `~/.local/state/omarchy-cast/` (encoder cache, portal restore token)
- Daemon idle timeout: 30 seconds with zero active sessions.
- Cast receiver app ID: `CC1AD845` (Default Media Receiver).
- Default encoder ranking: `["vaapi", "x264", "nvenc"]` — NVENC last, deliberately.
- License: MIT, matching `omarchy-prayer`.
- Tests: `pytest`. No test may require a real receiver, a network, a compositor, or a GPU.

## Field Findings (live test, 2026-08-05)

Verified on the target laptop against a real `AppleTV11,1` ("Living Room").
These change requirements; they are not optional polish.

**Confirmed working end to end:** install from AUR, daemon startup, control
socket, JSON protocol, mDNS discovery (on a network that forwards multicast),
direct-IP connect, SRP-6a PIN pairing, credential persistence to
`~/.config/doubletake/credentials.json` (mode 0600), and `pair-verify` on
reconnect.

**Not yet verified:** portal capture, encode, and an actual video frame reaching
a receiver. Everything in Tasks 10–12 remains unproven against hardware.

1. **mDNS discovery cannot be the only way to reach a device.** On one test
   network a device with AirPlay ports 7000/7100/5000 open was completely
   invisible to a 20-second multicast scan — the AP does not forward multicast
   between clients. Discovery-only UX is dead there. The CLI MUST accept a raw
   address (`omarchy-cast start --address 192.168.1.231`), and the walker menu
   MUST offer a manual-entry item. Treat this as a first-class path.

2. **Password-protected receivers are unsupported by doubletake 0.4.0.** A
   receiver with *Require Password* enabled (Settings → AirPlay and HomeKit)
   challenges the mirroring SETUP with RFC 2069 HTTP Digest auth. doubletake
   0.4.0 does not answer it and fails with:
   `mirror setup failed: SETUP phase 1 (audio): HTTP 401 (body: )`
   This is upstream issue #26, an open unmerged PR. Any shared or corporate
   Apple TV is likely to be configured this way. Requirements:
   - `AirPlayBackend` MUST detect `HTTP 401` in the failure and emit a specific
     message naming *Require Password* and upstream issue #26 — not a generic
     "setup failed". A user hitting this must not have to debug it.
   - Add a `code` field to the `[airplay]` config section now, plumbed to
     `DOUBLETAKE_CODE` in the daemon environment, so support lands as a config
     change once #26 merges. Do not add a CLI flag for it yet.

3. **`-no-audio` does not skip the audio SETUP phase.** The failure above
   occurred with the daemon started as `-no-audio`. Do not assume that flag
   removes the audio code path.

4. **A firewall with default-DROP INPUT blocks mirroring**, exactly as the
   design predicted. ufw was active on the test machine and rules scoped to the
   receiver's subnet were required before the receiver could connect back. The
   README instructions in Task 13 are load-bearing, not boilerplate.

5. **Pairing emits a benign scary log line.** `transient pairing failed:
   pair-setup M4 error: 2` appears immediately before the PIN prompt on every
   first pairing. Never surface it as an error.

---

### Task 1: Project scaffolding, Device model, and config

**Files:**
- Create: `pyproject.toml`
- Create: `omarchy_cast/__init__.py`
- Create: `omarchy_cast/core/__init__.py`
- Create: `omarchy_cast/core/device.py`
- Create: `omarchy_cast/core/config.py`
- Create: `LICENSE` (MIT, copyright Basem Aljedai)
- Test: `tests/test_device.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Device(id: str, name: str, address: str, port: int, protocol: str, model: str | None = None)` — frozen dataclass, `omarchy_cast.core.device`
  - `Device.make_id(protocol: str, unique: str) -> str`
  - `Config` dataclass with fields `fps: int`, `encoder: str`, `airplay_port_range: str`, `airplay_bitrate: int`, `cast_http_port: int`, `encoder_ranking: list[str]`
  - `load_config(path: Path | None = None) -> Config` — `omarchy_cast.core.config`

- [ ] **Step 1: Write the failing tests**

`tests/test_device.py`:

```python
import pytest

from omarchy_cast.core.device import Device


def test_make_id_namespaces_by_protocol():
    assert Device.make_id("cast", "abc-123") == "cast:abc-123"
    assert Device.make_id("airplay", "AA:BB") == "airplay:AA:BB"


def test_device_is_frozen():
    d = Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")
    with pytest.raises(Exception):
        d.name = "other"


def test_device_rejects_unknown_protocol():
    with pytest.raises(ValueError, match="unknown protocol"):
        Device(id="x:1", name="TV", address="1.2.3.4", port=1, protocol="bogus")
```

`tests/test_config.py`:

```python
from omarchy_cast.core.config import Config, load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg == Config()
    assert cfg.encoder_ranking == ["vaapi", "x264", "nvenc"]
    assert cfg.fps == 30


def test_file_overrides_only_named_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[capture]\nfps = 60\n')
    cfg = load_config(p)
    assert cfg.fps == 60
    assert cfg.encoder == "auto"


def test_rejects_unknown_encoder(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[capture]\nencoder = "quicksync"\n')
    try:
        load_config(p)
    except ValueError as e:
        assert "encoder" in str(e)
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_device.py tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast'`

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "omarchy-cast"
version = "0.1.0"
description = "Desktop mirroring for Omarchy to AirPlay and Google Cast receivers"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = ["PyChromecast>=14", "zeroconf>=0.140"]

[project.scripts]
omarchy-cast = "omarchy_cast.cli.main:main"
omarchy-castd = "omarchy_cast.core.daemon:main"

[tool.setuptools.packages.find]
include = ["omarchy_cast*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Write the minimal implementation**

`omarchy_cast/__init__.py` and `omarchy_cast/core/__init__.py`: empty files.

`omarchy_cast/core/device.py`:

```python
from dataclasses import dataclass

PROTOCOLS = ("airplay", "cast")


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    address: str
    port: int
    protocol: str
    model: str | None = None

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"unknown protocol: {self.protocol}")

    @staticmethod
    def make_id(protocol: str, unique: str) -> str:
        return f"{protocol}:{unique}"
```

`omarchy_cast/core/config.py`:

```python
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENCODERS = ("auto", "vaapi", "nvenc", "x264")
DEFAULT_RANKING = ["vaapi", "x264", "nvenc"]


def default_config_path() -> Path:
    return Path.home() / ".config" / "omarchy-cast" / "config.toml"


@dataclass
class Config:
    fps: int = 30
    encoder: str = "auto"
    encoder_ranking: list[str] = field(default_factory=lambda: list(DEFAULT_RANKING))
    airplay_port_range: str = "60000-60010"
    airplay_bitrate: int = 0
    cast_http_port: int = 0


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    cfg = Config()
    if not path.exists():
        return cfg

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    capture = data.get("capture", {})
    if "fps" in capture:
        cfg.fps = int(capture["fps"])
    if "encoder" in capture:
        cfg.encoder = str(capture["encoder"])
    if "encoder_ranking" in capture:
        cfg.encoder_ranking = list(capture["encoder_ranking"])

    airplay = data.get("airplay", {})
    if "port_range" in airplay:
        cfg.airplay_port_range = str(airplay["port_range"])
    if "bitrate" in airplay:
        cfg.airplay_bitrate = int(airplay["bitrate"])

    cast = data.get("cast", {})
    if "http_port" in cast:
        cfg.cast_http_port = int(cast["http_port"])

    if cfg.encoder not in ENCODERS:
        raise ValueError(f"invalid encoder: {cfg.encoder}; expected one of {ENCODERS}")
    return cfg
```

`LICENSE`: standard MIT text, `Copyright (c) 2026 Basem Aljedai`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_device.py tests/test_config.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml LICENSE omarchy_cast tests
git commit -m "feat: add Device model and config loading"
```

---

### Task 2: Session state machine

**Files:**
- Create: `omarchy_cast/core/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `Device` from Task 1.
- Produces:
  - `SessionState` — `StrEnum` with members `IDLE, CONNECTING, AWAITING_PIN, STREAMING, STOPPING, FAILED`
  - `Session(device: Device)` with `.state`, `.error`, `.transition(new: SessionState, error: str | None = None) -> None`, `.is_active` property
  - `InvalidTransition(Exception)`

- [ ] **Step 1: Write the failing test**

`tests/test_session.py`:

```python
import pytest

from omarchy_cast.core.device import Device
from omarchy_cast.core.session import InvalidTransition, Session, SessionState


def make_device():
    return Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")


def test_starts_idle():
    assert Session(make_device()).state is SessionState.IDLE


def test_happy_path_transitions():
    s = Session(make_device())
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.STREAMING)
    s.transition(SessionState.STOPPING)
    s.transition(SessionState.IDLE)
    assert s.state is SessionState.IDLE


def test_pin_path_allowed():
    s = Session(make_device())
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.AWAITING_PIN)
    s.transition(SessionState.STREAMING)
    assert s.state is SessionState.STREAMING


def test_can_fail_from_any_state():
    for start in (SessionState.CONNECTING, SessionState.AWAITING_PIN, SessionState.STREAMING):
        s = Session(make_device())
        s.transition(SessionState.CONNECTING)
        if start is not SessionState.CONNECTING:
            s.transition(start)
        s.transition(SessionState.FAILED, error="boom")
        assert s.state is SessionState.FAILED
        assert s.error == "boom"


def test_illegal_transition_raises():
    s = Session(make_device())
    with pytest.raises(InvalidTransition):
        s.transition(SessionState.STREAMING)


def test_error_cleared_on_leaving_failed():
    s = Session(make_device())
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.FAILED, error="boom")
    s.transition(SessionState.IDLE)
    assert s.error is None


def test_is_active_only_while_connecting_or_streaming():
    s = Session(make_device())
    assert not s.is_active
    s.transition(SessionState.CONNECTING)
    assert s.is_active
    s.transition(SessionState.STREAMING)
    assert s.is_active
    s.transition(SessionState.STOPPING)
    s.transition(SessionState.IDLE)
    assert not s.is_active
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.core.session'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/core/session.py`:

```python
import time
from enum import StrEnum

from omarchy_cast.core.device import Device


class SessionState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    AWAITING_PIN = "awaiting_pin"
    STREAMING = "streaming"
    STOPPING = "stopping"
    FAILED = "failed"


ALLOWED: dict[SessionState, set[SessionState]] = {
    SessionState.IDLE: {SessionState.CONNECTING},
    SessionState.CONNECTING: {SessionState.AWAITING_PIN, SessionState.STREAMING, SessionState.STOPPING},
    SessionState.AWAITING_PIN: {SessionState.STREAMING, SessionState.STOPPING},
    SessionState.STREAMING: {SessionState.STOPPING},
    SessionState.STOPPING: {SessionState.IDLE},
    SessionState.FAILED: {SessionState.IDLE, SessionState.CONNECTING},
}

ACTIVE = {SessionState.CONNECTING, SessionState.AWAITING_PIN, SessionState.STREAMING}


class InvalidTransition(Exception):
    pass


class Session:
    def __init__(self, device: Device) -> None:
        self.device = device
        self.state = SessionState.IDLE
        self.error: str | None = None
        self.started_at: float | None = None

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE

    def transition(self, new: SessionState, error: str | None = None) -> None:
        if new is not SessionState.FAILED and new not in ALLOWED[self.state]:
            raise InvalidTransition(f"{self.state} -> {new}")

        if new is SessionState.FAILED:
            self.error = error
        else:
            self.error = None

        if new is SessionState.STREAMING:
            self.started_at = time.monotonic()
        elif new in (SessionState.IDLE, SessionState.FAILED):
            self.started_at = None

        self.state = new
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/core/session.py tests/test_session.py
git commit -m "feat: add session state machine"
```

---

### Task 3: Encoder probing and ranking

**Files:**
- Create: `omarchy_cast/capture/__init__.py`
- Create: `omarchy_cast/capture/encoder.py`
- Test: `tests/test_encoder.py`

**Interfaces:**
- Consumes: `Config` from Task 1.
- Produces:
  - `ENCODER_ELEMENTS: dict[str, str]` mapping `"vaapi" -> "vah264enc"`, `"nvenc" -> "nvh264enc"`, `"x264" -> "x264enc"`
  - `probe_available(runner: Callable[[str], bool]) -> set[str]`
  - `select_encoder(config: Config, available: set[str]) -> str` — returns an encoder key, raises `NoEncoderAvailable` if none
  - `gst_element_for(encoder: str) -> str`
  - `NoEncoderAvailable(Exception)`

- [ ] **Step 1: Write the failing test**

`tests/test_encoder.py`:

```python
import pytest

from omarchy_cast.capture.encoder import (
    NoEncoderAvailable,
    gst_element_for,
    probe_available,
    select_encoder,
)
from omarchy_cast.core.config import Config


def test_probe_uses_runner_per_element():
    seen = []

    def runner(element: str) -> bool:
        seen.append(element)
        return element == "x264enc"

    assert probe_available(runner) == {"x264"}
    assert set(seen) == {"vah264enc", "nvh264enc", "x264enc"}


def test_select_prefers_vaapi_over_nvenc_by_default():
    cfg = Config()
    assert select_encoder(cfg, {"vaapi", "nvenc", "x264"}) == "vaapi"


def test_select_falls_back_to_x264_before_nvenc():
    cfg = Config()
    assert select_encoder(cfg, {"nvenc", "x264"}) == "x264"


def test_explicit_encoder_is_honoured():
    cfg = Config(encoder="nvenc")
    assert select_encoder(cfg, {"vaapi", "nvenc"}) == "nvenc"


def test_explicit_encoder_missing_raises():
    cfg = Config(encoder="nvenc")
    with pytest.raises(NoEncoderAvailable, match="nvenc"):
        select_encoder(cfg, {"x264"})


def test_no_encoders_at_all_raises():
    with pytest.raises(NoEncoderAvailable):
        select_encoder(Config(), set())


def test_custom_ranking_respected():
    cfg = Config(encoder_ranking=["nvenc", "vaapi", "x264"])
    assert select_encoder(cfg, {"vaapi", "nvenc"}) == "nvenc"


def test_gst_element_lookup():
    assert gst_element_for("vaapi") == "vah264enc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_encoder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.capture'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/capture/__init__.py`: empty file.

`omarchy_cast/capture/encoder.py`:

```python
import shutil
import subprocess
from collections.abc import Callable

from omarchy_cast.core.config import Config

ENCODER_ELEMENTS = {
    "vaapi": "vah264enc",
    "nvenc": "nvh264enc",
    "x264": "x264enc",
}


class NoEncoderAvailable(Exception):
    pass


def gst_element_for(encoder: str) -> str:
    return ENCODER_ELEMENTS[encoder]


def gst_inspect_runner(element: str) -> bool:
    """Real probe: gst-inspect-1.0 exits non-zero for unknown elements."""
    binary = shutil.which("gst-inspect-1.0")
    if binary is None:
        return False
    result = subprocess.run(
        [binary, element],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def probe_available(runner: Callable[[str], bool] = gst_inspect_runner) -> set[str]:
    return {key for key, element in ENCODER_ELEMENTS.items() if runner(element)}


def select_encoder(config: Config, available: set[str]) -> str:
    if config.encoder != "auto":
        if config.encoder not in available:
            raise NoEncoderAvailable(
                f"configured encoder {config.encoder!r} is not available; "
                f"found: {sorted(available) or 'none'}"
            )
        return config.encoder

    for candidate in config.encoder_ranking:
        if candidate in available:
            return candidate

    raise NoEncoderAvailable(
        "no H.264 encoder found; install gst-plugin-va or gst-plugins-ugly"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_encoder.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/capture tests/test_encoder.py
git commit -m "feat: add encoder probing and ranking"
```

---

### Task 4: mDNS discovery

**Files:**
- Create: `omarchy_cast/core/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: `Device` from Task 1.
- Produces:
  - `AIRPLAY_TYPE = "_airplay._tcp.local."`, `CAST_TYPE = "_googlecast._tcp.local."`
  - `device_from_airplay(info) -> Device | None`
  - `device_from_cast(info) -> Device | None`
  - `Discovery(zeroconf)` with `.devices() -> list[Device]`, `.start()`, `.stop()`

`info` is a `zeroconf.ServiceInfo`-shaped object: attributes `name: str`, `port: int`, `properties: dict[bytes, bytes]`, and method `parsed_addresses() -> list[str]`.

- [ ] **Step 1: Write the failing test**

`tests/test_discovery.py`:

```python
from omarchy_cast.core.discovery import device_from_airplay, device_from_cast


class FakeInfo:
    def __init__(self, name, port, properties, addresses):
        self.name = name
        self.port = port
        self.properties = properties
        self._addresses = addresses

    def parsed_addresses(self):
        return self._addresses


def test_airplay_device_parsed():
    info = FakeInfo(
        name="Living Room._airplay._tcp.local.",
        port=7000,
        properties={b"deviceid": b"AA:BB:CC:DD:EE:FF", b"model": b"AppleTV14,1"},
        addresses=["192.168.1.77"],
    )
    d = device_from_airplay(info)
    assert d.id == "airplay:AA:BB:CC:DD:EE:FF"
    assert d.name == "Living Room"
    assert d.address == "192.168.1.77"
    assert d.port == 7000
    assert d.protocol == "airplay"
    assert d.model == "AppleTV14,1"


def test_cast_device_parsed():
    info = FakeInfo(
        name="Chromecast-abc._googlecast._tcp.local.",
        port=8009,
        properties={b"id": b"abc123", b"fn": b"Bedroom TV", b"md": b"Chromecast"},
        addresses=["192.168.1.50"],
    )
    d = device_from_cast(info)
    assert d.id == "cast:abc123"
    assert d.name == "Bedroom TV"
    assert d.address == "192.168.1.50"
    assert d.protocol == "cast"
    assert d.model == "Chromecast"


def test_device_without_address_is_skipped():
    info = FakeInfo("X._airplay._tcp.local.", 7000, {b"deviceid": b"A"}, [])
    assert device_from_airplay(info) is None


def test_airplay_without_deviceid_falls_back_to_name():
    info = FakeInfo("Studio._airplay._tcp.local.", 7000, {}, ["10.0.0.2"])
    d = device_from_airplay(info)
    assert d.id == "airplay:Studio"


def test_cast_without_friendly_name_falls_back_to_service_name():
    info = FakeInfo("Chromecast-xyz._googlecast._tcp.local.", 8009, {b"id": b"xyz"}, ["10.0.0.3"])
    d = device_from_cast(info)
    assert d.name == "Chromecast-xyz"


def test_ipv6_only_device_is_skipped():
    info = FakeInfo("X._googlecast._tcp.local.", 8009, {b"id": b"q"}, ["fe80::1"])
    assert device_from_cast(info) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.core.discovery'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/core/discovery.py`:

```python
import ipaddress
import logging
import threading

from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

from omarchy_cast.core.device import Device

AIRPLAY_TYPE = "_airplay._tcp.local."
CAST_TYPE = "_googlecast._tcp.local."

log = logging.getLogger(__name__)


def _text(properties: dict, key: bytes) -> str | None:
    value = properties.get(key)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _first_ipv4(info) -> str | None:
    for address in info.parsed_addresses():
        try:
            if isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
                return address
        except ValueError:
            continue
    return None


def _short_name(service_name: str) -> str:
    return service_name.split(".")[0]


def device_from_airplay(info) -> Device | None:
    address = _first_ipv4(info)
    if address is None:
        return None
    name = _short_name(info.name)
    unique = _text(info.properties, b"deviceid") or name
    return Device(
        id=Device.make_id("airplay", unique),
        name=name,
        address=address,
        port=info.port,
        protocol="airplay",
        model=_text(info.properties, b"model"),
    )


def device_from_cast(info) -> Device | None:
    address = _first_ipv4(info)
    if address is None:
        return None
    service_name = _short_name(info.name)
    unique = _text(info.properties, b"id") or service_name
    return Device(
        id=Device.make_id("cast", unique),
        name=_text(info.properties, b"fn") or service_name,
        address=address,
        port=info.port,
        protocol="cast",
        model=_text(info.properties, b"md"),
    )


PARSERS = {AIRPLAY_TYPE: device_from_airplay, CAST_TYPE: device_from_cast}


class Discovery:
    """Owns one Zeroconf instance and browses both service types."""

    def __init__(self, zeroconf: Zeroconf | None = None) -> None:
        self._zeroconf = zeroconf or Zeroconf()
        self._owns_zeroconf = zeroconf is None
        self._devices: dict[str, Device] = {}
        self._lock = threading.Lock()
        self._browser: ServiceBrowser | None = None

    @property
    def zeroconf(self) -> Zeroconf:
        return self._zeroconf

    def start(self) -> None:
        self._browser = ServiceBrowser(
            self._zeroconf, list(PARSERS), handlers=[self._on_change]
        )

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._owns_zeroconf:
            self._zeroconf.close()

    def devices(self) -> list[Device]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: (d.protocol, d.name))

    def _on_change(self, zeroconf, service_type, name, state_change) -> None:
        parser = PARSERS.get(service_type)
        if parser is None:
            return

        if state_change is ServiceStateChange.Removed:
            with self._lock:
                for key, device in list(self._devices.items()):
                    if device.name == _short_name(name):
                        del self._devices[key]
            return

        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if info is None:
            return
        try:
            device = parser(info)
        except ValueError:
            log.debug("ignoring malformed service %s", name)
            return
        if device is not None:
            with self._lock:
                self._devices[device.id] = device
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_discovery.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/core/discovery.py tests/test_discovery.py
git commit -m "feat: add shared mDNS discovery for AirPlay and Cast"
```

---

### Task 5: Backend interface and stub backend

**Files:**
- Create: `omarchy_cast/backends/__init__.py`
- Create: `omarchy_cast/backends/base.py`
- Create: `omarchy_cast/backends/stub.py`
- Test: `tests/test_backend_base.py`

**Interfaces:**
- Consumes: `Device` (Task 1), `SessionState` (Task 2).
- Produces:
  - `StateCallback = Callable[[Device, SessionState, str | None], None]`
  - `BackendError(Exception)`
  - `Backend` ABC: class attribute `protocol: str`; `__init__(self, on_state: StateCallback)`; `async def start(self, device: Device) -> None`; `async def stop(self, device: Device) -> None`; `async def submit_pin(self, device: Device, pin: str) -> None`; `async def shutdown(self) -> None`
  - `StubBackend(on_state, *, fail_with: str | None = None, needs_pin: bool = False)` — used by daemon tests, ships in the package so tests need no fixtures directory

- [ ] **Step 1: Write the failing test**

`tests/test_backend_base.py`:

```python
import pytest

from omarchy_cast.backends.base import Backend, BackendError
from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


def make_device():
    return Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")


def test_backend_is_abstract():
    with pytest.raises(TypeError):
        Backend(lambda *a: None)


@pytest.mark.asyncio
async def test_stub_reports_connecting_then_streaming():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append((s, e)))
    await backend.start(make_device())
    assert seen == [
        (SessionState.CONNECTING, None),
        (SessionState.STREAMING, None),
    ]


@pytest.mark.asyncio
async def test_stub_can_simulate_failure():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append((s, e)), fail_with="nope")
    with pytest.raises(BackendError, match="nope"):
        await backend.start(make_device())
    assert seen[-1] == (SessionState.FAILED, "nope")


@pytest.mark.asyncio
async def test_stub_pin_flow():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append(s), needs_pin=True)
    device = make_device()
    await backend.start(device)
    assert seen[-1] is SessionState.AWAITING_PIN
    await backend.submit_pin(device, "1234")
    assert seen[-1] is SessionState.STREAMING


@pytest.mark.asyncio
async def test_stub_stop_returns_to_idle():
    seen = []
    backend = StubBackend(lambda d, s, e: seen.append(s))
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert seen[-2:] == [SessionState.STOPPING, SessionState.IDLE]
```

- [ ] **Step 2: Add pytest-asyncio and run to verify failure**

Install: `sudo pacman -S --needed python-pytest-asyncio`

Append to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
asyncio_mode = "auto"
```

Run: `pytest tests/test_backend_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.backends'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/backends/__init__.py`: empty file.

`omarchy_cast/backends/base.py`:

```python
from abc import ABC, abstractmethod
from collections.abc import Callable

from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState

StateCallback = Callable[[Device, SessionState, str | None], None]


class BackendError(Exception):
    """Raised when a backend cannot start, stop, or sustain a session.

    The message is shown directly to the user, so it must be actionable.
    """


class Backend(ABC):
    protocol: str = ""

    def __init__(self, on_state: StateCallback) -> None:
        self._on_state = on_state

    def _emit(self, device: Device, state: SessionState, error: str | None = None) -> None:
        self._on_state(device, state, error)

    @abstractmethod
    async def start(self, device: Device) -> None: ...

    @abstractmethod
    async def stop(self, device: Device) -> None: ...

    async def submit_pin(self, device: Device, pin: str) -> None:
        raise BackendError(f"{self.protocol} does not use PIN pairing")

    async def shutdown(self) -> None:
        return None
```

`omarchy_cast/backends/stub.py`:

```python
from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


class StubBackend(Backend):
    """In-memory backend used to test the daemon without hardware."""

    protocol = "cast"

    def __init__(
        self,
        on_state: StateCallback,
        *,
        fail_with: str | None = None,
        needs_pin: bool = False,
    ) -> None:
        super().__init__(on_state)
        self._fail_with = fail_with
        self._needs_pin = needs_pin

    async def start(self, device: Device) -> None:
        self._emit(device, SessionState.CONNECTING)
        if self._fail_with:
            self._emit(device, SessionState.FAILED, self._fail_with)
            raise BackendError(self._fail_with)
        if self._needs_pin:
            self._emit(device, SessionState.AWAITING_PIN)
            return
        self._emit(device, SessionState.STREAMING)

    async def submit_pin(self, device: Device, pin: str) -> None:
        self._emit(device, SessionState.STREAMING)

    async def stop(self, device: Device) -> None:
        self._emit(device, SessionState.STOPPING)
        self._emit(device, SessionState.IDLE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_backend_base.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/backends pyproject.toml tests/test_backend_base.py
git commit -m "feat: add Backend interface and stub backend"
```

---

### Task 6: Daemon socket protocol and server

**Files:**
- Create: `omarchy_cast/core/protocol.py`
- Create: `omarchy_cast/core/daemon.py`
- Test: `tests/test_protocol.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `Device`, `Session`, `SessionState`, `Backend`, `StubBackend`, `Discovery`.
- Produces:
  - `encode_request(cmd: str, **kwargs) -> bytes`, `decode_line(line: bytes) -> dict` — `omarchy_cast.core.protocol`
  - `ok(data) -> dict`, `err(message: str) -> dict`
  - `socket_path() -> Path`
  - `Daemon(discovery, backends: dict[str, Backend], idle_timeout: float = 30.0)` with `.handle(request: dict) -> dict`, `.serve(path)`, `.sessions: dict[str, Session]`
  - `main()` entry point

Commands: `list`, `start` (`device_id`), `stop` (`device_id` optional — omitted stops all), `status`, `pin` (`device_id`, `pin`), `quit`.

- [ ] **Step 1: Write the failing tests**

`tests/test_protocol.py`:

```python
import pytest

from omarchy_cast.core.protocol import decode_line, encode_request, err, ok


def test_roundtrip():
    line = encode_request("start", device_id="cast:1")
    assert decode_line(line) == {"cmd": "start", "device_id": "cast:1"}


def test_encode_ends_with_newline():
    assert encode_request("list").endswith(b"\n")


def test_decode_rejects_non_object():
    with pytest.raises(ValueError, match="object"):
        decode_line(b'["nope"]\n')


def test_decode_rejects_missing_cmd():
    with pytest.raises(ValueError, match="cmd"):
        decode_line(b'{"device_id": "x"}\n')


def test_ok_and_err_shapes():
    assert ok({"a": 1}) == {"ok": True, "data": {"a": 1}}
    assert err("bad") == {"ok": False, "error": "bad"}
```

`tests/test_daemon.py`:

```python
import pytest

from omarchy_cast.backends.stub import StubBackend
from omarchy_cast.core.daemon import Daemon
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


class FakeDiscovery:
    def __init__(self, devices):
        self._devices = devices

    def devices(self):
        return self._devices

    def start(self):
        pass

    def stop(self):
        pass


def make_device(protocol="cast", ident="1"):
    return Device(
        id=Device.make_id(protocol, ident),
        name=f"{protocol}-{ident}",
        address="192.168.1.5",
        port=8009,
        protocol=protocol,
    )


def make_daemon(devices=None, **stub_kwargs):
    devices = devices if devices is not None else [make_device()]
    daemon = Daemon(FakeDiscovery(devices), {})
    backend = StubBackend(daemon.on_state, **stub_kwargs)
    daemon.backends["cast"] = backend
    return daemon


async def test_list_returns_devices():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "list"})
    assert resp["ok"] is True
    assert resp["data"]["devices"][0]["id"] == "cast:1"


async def test_start_creates_streaming_session():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is True
    assert daemon.sessions["cast:1"].state is SessionState.STREAMING


async def test_start_unknown_device_errors():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:999"})
    assert resp["ok"] is False
    assert "not found" in resp["error"]


async def test_backend_failure_surfaces_message():
    daemon = make_daemon(fail_with="firewall blocked")
    resp = await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert resp["ok"] is False
    assert "firewall blocked" in resp["error"]


async def test_status_reports_active_session():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    resp = await daemon.handle({"cmd": "status"})
    sessions = resp["data"]["sessions"]
    assert sessions[0]["state"] == "streaming"
    assert sessions[0]["name"] == "cast-1"


async def test_stop_without_device_stops_all():
    daemon = make_daemon()
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    resp = await daemon.handle({"cmd": "stop"})
    assert resp["ok"] is True
    assert daemon.sessions == {}


async def test_pin_flow_reaches_streaming():
    daemon = make_daemon(needs_pin=True)
    await daemon.handle({"cmd": "start", "device_id": "cast:1"})
    assert daemon.sessions["cast:1"].state is SessionState.AWAITING_PIN
    resp = await daemon.handle({"cmd": "pin", "device_id": "cast:1", "pin": "1234"})
    assert resp["ok"] is True
    assert daemon.sessions["cast:1"].state is SessionState.STREAMING


async def test_unknown_command_errors():
    daemon = make_daemon()
    resp = await daemon.handle({"cmd": "frobnicate"})
    assert resp["ok"] is False
    assert "unknown command" in resp["error"]


async def test_no_backend_for_protocol_errors():
    daemon = make_daemon(devices=[make_device("airplay", "9")])
    resp = await daemon.handle({"cmd": "start", "device_id": "airplay:9"})
    assert resp["ok"] is False
    assert "no backend" in resp["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_protocol.py tests/test_daemon.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.core.protocol'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/core/protocol.py`:

```python
import json
import os
from pathlib import Path
from typing import Any


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "omarchy-cast.sock"


def encode_request(cmd: str, **kwargs: Any) -> bytes:
    payload = {"cmd": cmd, **{k: v for k, v in kwargs.items() if v is not None}}
    return (json.dumps(payload) + "\n").encode("utf-8")


def encode_response(response: dict) -> bytes:
    return (json.dumps(response) + "\n").encode("utf-8")


def decode_line(line: bytes) -> dict:
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    if "cmd" not in data:
        raise ValueError("request missing 'cmd'")
    return data


def ok(data: Any = None) -> dict:
    return {"ok": True, "data": data}


def err(message: str) -> dict:
    return {"ok": False, "error": message}
```

`omarchy_cast/core/daemon.py`:

```python
import asyncio
import contextlib
import logging
import os
import time

from omarchy_cast.backends.base import Backend, BackendError
from omarchy_cast.core.device import Device
from omarchy_cast.core.protocol import (
    decode_line,
    encode_response,
    err,
    ok,
    socket_path,
)
from omarchy_cast.core.session import Session, SessionState

log = logging.getLogger(__name__)


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
        devices = [
            {
                "id": d.id,
                "name": d.name,
                "protocol": d.protocol,
                "address": d.address,
                "model": d.model,
            }
            for d in self.discovery.devices()
        ]
        return ok({"devices": devices})

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
        targets = (
            [self.sessions[device_id]] if device_id in self.sessions
            else list(self.sessions.values()) if device_id is None
            else []
        )
        if device_id is not None and not targets:
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
    """Entry point wired up fully in Task 12."""
    raise SystemExit("omarchy-castd is not wired up until Task 12")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_protocol.py tests/test_daemon.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/core/protocol.py omarchy_cast/core/daemon.py tests/test_protocol.py tests/test_daemon.py
git commit -m "feat: add daemon socket protocol and request handling"
```

---

### Task 7: CLI client with daemon auto-spawn

**Files:**
- Create: `omarchy_cast/cli/__init__.py`
- Create: `omarchy_cast/cli/client.py`
- Create: `omarchy_cast/cli/main.py`
- Test: `tests/test_client.py`
- Test: `tests/test_cli_main.py`

**Interfaces:**
- Consumes: `encode_request`, `socket_path` from Task 6.
- Produces:
  - `async def request(cmd: str, path: Path | None = None, *, autospawn: bool = True, **kwargs) -> dict` — `omarchy_cast.cli.client`
  - `DaemonUnavailable(Exception)`
  - `main(argv: list[str] | None = None) -> int` — `omarchy_cast.cli.main`

CLI surface: `omarchy-cast list`, `start <device-id>`, `stop [device-id]`, `status`, `pin <device-id> <pin>`.

- [ ] **Step 1: Write the failing tests**

`tests/test_client.py`:

```python
import asyncio
import json

import pytest

from omarchy_cast.cli.client import DaemonUnavailable, request


async def run_echo_server(path, response):
    async def handle(reader, writer):
        await reader.readline()
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_request_roundtrip(tmp_path):
    sock = tmp_path / "test.sock"
    server = await run_echo_server(sock, {"ok": True, "data": {"devices": []}})
    try:
        resp = await request("list", path=sock)
        assert resp == {"ok": True, "data": {"devices": []}}
    finally:
        server.close()
        await server.wait_closed()


async def test_missing_socket_without_autospawn_raises(tmp_path):
    with pytest.raises(DaemonUnavailable):
        await request("list", path=tmp_path / "nope.sock", autospawn=False)
```

`tests/test_cli_main.py`:

```python
import pytest

from omarchy_cast.cli import main as cli_main


@pytest.fixture
def fake_request(monkeypatch):
    calls = []

    async def _request(cmd, path=None, **kwargs):
        calls.append((cmd, kwargs))
        return calls_response[cmd]

    calls_response = {}
    monkeypatch.setattr(cli_main, "request", _request)
    return calls, calls_response


def test_list_prints_devices(fake_request, capsys):
    calls, responses = fake_request
    responses["list"] = {
        "ok": True,
        "data": {"devices": [{"id": "cast:1", "name": "TV", "protocol": "cast", "model": "Chromecast"}]},
    }
    assert cli_main.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "cast:1" in out and "TV" in out


def test_start_passes_device_id(fake_request):
    calls, responses = fake_request
    responses["start"] = {"ok": True, "data": {"state": "streaming"}}
    assert cli_main.main(["start", "cast:1"]) == 0
    assert calls[0] == ("start", {"device_id": "cast:1"})


def test_error_response_returns_nonzero_and_prints_to_stderr(fake_request, capsys):
    calls, responses = fake_request
    responses["start"] = {"ok": False, "error": "firewall blocked"}
    assert cli_main.main(["start", "cast:1"]) == 1
    assert "firewall blocked" in capsys.readouterr().err


def test_stop_without_device_sends_no_device_id(fake_request):
    calls, responses = fake_request
    responses["stop"] = {"ok": True, "data": {"stopped": 1}}
    assert cli_main.main(["stop"]) == 0
    assert calls[0] == ("stop", {"device_id": None})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_client.py tests/test_cli_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.cli'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/cli/__init__.py`: empty file.

`omarchy_cast/cli/client.py`:

```python
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from omarchy_cast.core.protocol import encode_request, socket_path

SPAWN_TIMEOUT = 5.0


class DaemonUnavailable(Exception):
    pass


def _spawn_daemon() -> None:
    subprocess.Popen(
        [sys.executable, "-m", "omarchy_cast.core.daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


async def _wait_for_socket(path: Path, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise DaemonUnavailable(f"daemon did not create {path} within {timeout}s")


async def request(
    cmd: str,
    path: Path | None = None,
    *,
    autospawn: bool = True,
    **kwargs: Any,
) -> dict:
    path = path or socket_path()

    if not path.exists():
        if not autospawn:
            raise DaemonUnavailable(f"no daemon socket at {path}")
        _spawn_daemon()
        await _wait_for_socket(path, SPAWN_TIMEOUT)

    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        raise DaemonUnavailable(f"cannot reach daemon at {path}: {exc}") from exc

    try:
        writer.write(encode_request(cmd, **kwargs))
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()

    if not line:
        raise DaemonUnavailable("daemon closed the connection without responding")
    return json.loads(line.decode("utf-8"))
```

`omarchy_cast/cli/main.py`:

```python
import argparse
import asyncio
import sys

from omarchy_cast.cli.client import DaemonUnavailable, request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omarchy-cast")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list discovered receivers")
    sub.add_parser("status", help="show active sessions")

    start = sub.add_parser("start", help="start mirroring to a receiver")
    start.add_argument("device_id")

    stop = sub.add_parser("stop", help="stop mirroring (all sessions if no device given)")
    stop.add_argument("device_id", nargs="?", default=None)

    pin = sub.add_parser("pin", help="submit a pairing PIN")
    pin.add_argument("device_id")
    pin.add_argument("pin")

    return parser


def _print_devices(devices: list[dict]) -> None:
    if not devices:
        print("no receivers found")
        return
    for d in devices:
        model = f" ({d['model']})" if d.get("model") else ""
        print(f"{d['id']:<28} {d['name']}{model}")


def _print_sessions(sessions: list[dict]) -> None:
    if not sessions:
        print("not casting")
        return
    for s in sessions:
        suffix = f" - {s['error']}" if s.get("error") else ""
        print(f"{s['name']} [{s['protocol']}] {s['state']}{suffix}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    kwargs: dict = {}
    if args.command in ("start", "stop", "pin"):
        kwargs["device_id"] = args.device_id
    if args.command == "pin":
        kwargs["pin"] = args.pin

    try:
        response = asyncio.run(request(args.command, **kwargs))
    except DaemonUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not response.get("ok"):
        print(response.get("error", "unknown error"), file=sys.stderr)
        return 1

    data = response.get("data") or {}
    if args.command == "list":
        _print_devices(data.get("devices", []))
    elif args.command == "status":
        _print_sessions(data.get("sessions", []))
    elif args.command == "stop":
        print(f"stopped {data.get('stopped', 0)} session(s)")
    else:
        print(data.get("state", "ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_client.py tests/test_cli_main.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/cli tests/test_client.py tests/test_cli_main.py
git commit -m "feat: add CLI client with daemon auto-spawn"
```

---

### Task 8: waybar indicator and walker menu

**Files:**
- Create: `omarchy_cast/cli/waybar.py`
- Create: `omarchy_cast/cli/menu.py`
- Modify: `omarchy_cast/cli/main.py` (add `waybar` and `menu` subcommands)
- Create: `share/waybar/cast-indicator.jsonc`
- Test: `tests/test_waybar.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: `request` from Task 7.
- Produces:
  - `render(sessions: list[dict]) -> dict` — `omarchy_cast.cli.waybar`; returns waybar JSON with keys `text`, `tooltip`, `class`
  - `format_entries(devices: list[dict]) -> list[str]` and `parse_selection(line: str) -> str | None` — `omarchy_cast.cli.menu`

The indicator must stay visible in both states, colour-coded rather than hidden — matching the existing `custom/idle-indicator` and `custom/screensaver-indicator` convention in this waybar config.

- [ ] **Step 1: Write the failing tests**

`tests/test_waybar.py`:

```python
from omarchy_cast.cli.waybar import render


def test_idle_state_is_visible_not_blank():
    out = render([])
    assert out["text"] != ""
    assert out["class"] == "idle"
    assert "not casting" in out["tooltip"].lower()


def test_streaming_shows_device_name():
    out = render([{"name": "Living Room", "protocol": "airplay", "state": "streaming", "error": None}])
    assert "Living Room" in out["tooltip"]
    assert out["class"] == "streaming"


def test_failed_state_surfaces_error():
    out = render([{"name": "TV", "protocol": "cast", "state": "failed", "error": "firewall blocked"}])
    assert out["class"] == "failed"
    assert "firewall blocked" in out["tooltip"]


def test_multiple_sessions_counted():
    out = render([
        {"name": "A", "protocol": "cast", "state": "streaming", "error": None},
        {"name": "B", "protocol": "airplay", "state": "streaming", "error": None},
    ])
    assert "2" in out["text"]


def test_connecting_is_its_own_class():
    out = render([{"name": "A", "protocol": "cast", "state": "connecting", "error": None}])
    assert out["class"] == "connecting"
```

`tests/test_menu.py`:

```python
from omarchy_cast.cli.menu import format_entries, parse_selection


def test_entries_are_grouped_and_labelled():
    entries = format_entries([
        {"id": "cast:1", "name": "Bedroom", "protocol": "cast", "model": "Chromecast"},
        {"id": "airplay:2", "name": "Living Room", "protocol": "airplay", "model": "AppleTV14,1"},
    ])
    assert any("Living Room" in e and "airplay:2" in e for e in entries)
    assert any("Bedroom" in e and "cast:1" in e for e in entries)


def test_selection_round_trips_to_device_id():
    entries = format_entries([{"id": "cast:1", "name": "Bedroom", "protocol": "cast", "model": None}])
    assert parse_selection(entries[0]) == "cast:1"


def test_empty_selection_returns_none():
    assert parse_selection("") is None
    assert parse_selection("   ") is None


def test_garbage_selection_returns_none():
    assert parse_selection("no id here") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_waybar.py tests/test_menu.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.cli.waybar'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/cli/waybar.py`:

```python
ICON_IDLE = "󰄡"
ICON_ACTIVE = "󰄠"


def render(sessions: list[dict]) -> dict:
    if not sessions:
        return {"text": ICON_IDLE, "tooltip": "Not casting", "class": "idle"}

    failed = [s for s in sessions if s.get("state") == "failed"]
    if failed:
        reason = failed[0].get("error") or "unknown error"
        return {
            "text": ICON_ACTIVE,
            "tooltip": f"Cast failed: {reason}",
            "class": "failed",
        }

    connecting = [s for s in sessions if s.get("state") in ("connecting", "awaiting_pin")]
    if connecting:
        return {
            "text": ICON_ACTIVE,
            "tooltip": f"Connecting to {connecting[0]['name']}...",
            "class": "connecting",
        }

    names = ", ".join(s["name"] for s in sessions)
    text = ICON_ACTIVE if len(sessions) == 1 else f"{ICON_ACTIVE} {len(sessions)}"
    return {"text": text, "tooltip": f"Casting to {names}", "class": "streaming"}
```

`omarchy_cast/cli/menu.py`:

```python
import re

LABELS = {"airplay": "AirPlay", "cast": "Chromecast"}
ID_PATTERN = re.compile(r"\[((?:airplay|cast):[^\]]+)\]$")


def format_entries(devices: list[dict]) -> list[str]:
    ordered = sorted(devices, key=lambda d: (d["protocol"] != "airplay", d["name"].lower()))
    entries = []
    for d in ordered:
        label = LABELS.get(d["protocol"], d["protocol"])
        model = f" · {d['model']}" if d.get("model") else ""
        entries.append(f"{d['name']} ({label}{model}) [{d['id']}]")
    return entries


def parse_selection(line: str) -> str | None:
    match = ID_PATTERN.search(line.strip())
    return match.group(1) if match else None
```

- [ ] **Step 4: Wire both into the CLI**

In `omarchy_cast/cli/main.py`, add to `build_parser()` after the `pin` parser:

```python
    sub.add_parser("waybar", help="print waybar JSON for the cast indicator")
    sub.add_parser("menu", help="pick a receiver via walker and start casting")
```

Add these imports at the top of `main.py`:

```python
import json
import subprocess

from omarchy_cast.cli.menu import format_entries, parse_selection
from omarchy_cast.cli.waybar import render
```

Add these two functions to `main.py`:

```python
def _run_waybar() -> int:
    try:
        response = asyncio.run(request("status", autospawn=False))
    except DaemonUnavailable:
        print(json.dumps(render([])))
        return 0
    sessions = (response.get("data") or {}).get("sessions", [])
    print(json.dumps(render(sessions)))
    return 0


def _run_menu() -> int:
    response = asyncio.run(request("list"))
    if not response.get("ok"):
        print(response.get("error", "unknown error"), file=sys.stderr)
        return 1

    devices = (response.get("data") or {}).get("devices", [])
    if not devices:
        subprocess.run(["notify-send", "omarchy-cast", "No receivers found"], check=False)
        return 1

    entries = format_entries(devices)
    picked = subprocess.run(
        ["walker", "--dmenu", "-p", "Cast to"],
        input="\n".join(entries),
        capture_output=True,
        text=True,
        check=False,
    )
    device_id = parse_selection(picked.stdout)
    if device_id is None:
        return 0

    result = asyncio.run(request("start", device_id=device_id))
    if not result.get("ok"):
        message = result.get("error", "unknown error")
        subprocess.run(["notify-send", "-u", "critical", "omarchy-cast", message], check=False)
        print(message, file=sys.stderr)
        return 1
    return 0
```

In `main()`, insert this immediately after the `args = build_parser().parse_args(argv)` line:

```python
    if args.command == "waybar":
        return _run_waybar()
    if args.command == "menu":
        return _run_menu()
```

- [ ] **Step 5: Write the waybar snippet**

`share/waybar/cast-indicator.jsonc`:

```jsonc
// Add to ~/.config/waybar/config.jsonc modules-right, and merge this block in.
"custom/cast-indicator": {
  "exec": "omarchy-cast waybar",
  "return-type": "json",
  "interval": 2,
  "on-click": "omarchy-cast menu",
  "on-click-right": "omarchy-cast stop",
  "tooltip": true
}
```

Add to `share/waybar/cast-indicator.css`:

```css
/* Toggle icons stay visible in both states; colour carries the state. */
#custom-cast-indicator { margin-right: 10px; }
#custom-cast-indicator.idle { opacity: 0.5; }
#custom-cast-indicator.connecting { color: @yellow; }
#custom-cast-indicator.streaming { color: @green; }
#custom-cast-indicator.failed { color: @red; }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_waybar.py tests/test_menu.py -v`
Expected: PASS (9 tests)

- [ ] **Step 7: Verify the full suite still passes**

Run: `pytest -v`
Expected: PASS, all tests green

- [ ] **Step 8: Commit**

```bash
git add omarchy_cast/cli share/waybar tests/test_waybar.py tests/test_menu.py
git commit -m "feat: add waybar indicator and walker menu front-ends"
```

---

### Task 9: AirPlay backend via doubletake

**Files:**
- Create: `omarchy_cast/backends/airplay.py`
- Test: `tests/test_airplay_backend.py`

**Interfaces:**
- Consumes: `Backend`, `BackendError`, `StateCallback` (Task 5); `Config` (Task 1).
- Produces:
  - `AirPlayBackend(on_state, config, runner: CommandRunner | None = None, poll_interval: float = 0.5)` with `protocol = "airplay"`
  - `CommandRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]` returning `(returncode, stdout, stderr)`
  - `DT_STATES = ("idle", "discovering", "connecting", "streaming", "pin_required")`
  - `STATE_MAP: dict[str, SessionState]` translating doubletake states to ours
  - `parse_ctl(stdout: str) -> dict` — parses the JSON envelope, raising `BackendError` on malformed output

**Verified contract (live test, 2026-08-05, against AppleTV14,1).** These facts were
confirmed against a real device and the daemon source (`internal/daemon/daemon.go`);
do not re-derive them:

- `doubletake-ctl` emits a **JSON** envelope on stdout, never free text:
  `{ok, state, device, device_ip, has_audio, audio_muted, needs_pin, error, devices[], streams[]}`
- States are exactly `idle`, `discovering`, `connecting`, `streaming`, `pin_required`.
  **There is no failed/error state** — failures come back as `ok: false` with an
  `error` string, and the reported state falls back to `idle`.
- **`connect` is asynchronous.** It returns immediately with `state: "connecting"`.
  The backend MUST poll `status` until the state reaches `streaming` or
  `pin_required`, or a timeout expires. A synchronous implementation will report
  success before the receiver has accepted anything.
- `pin` takes only the PIN, no device argument: `doubletake-ctl pin <PIN>`.
- `disconnect` with no argument stops all streams; with an IP, only that one.
- Pairing against a previously-unpaired device emits
  `transient pairing failed: pair-setup M4 error: 2` to the daemon log before
  entering `pin_required`. This is normal, not an error to surface.

Install dependencies first. Note that **doubletake does not pull `gst-plugin-va`**,
so `vah264enc` — the default encoder from Task 3 — is missing on a fresh install:

```bash
yay -S --needed doubletake gst-plugin-va
```

- [ ] **Step 1: Write the failing test**

`tests/test_airplay_backend.py`:

```python
import json

import pytest

from omarchy_cast.backends.airplay import AirPlayBackend
from omarchy_cast.backends.base import BackendError
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


def make_device():
    return Device(
        id="airplay:AA", name="Living Room", address="192.168.1.77",
        port=7000, protocol="airplay",
    )


def envelope(state="idle", ok=True, **extra):
    """Build a doubletake-ctl JSON envelope exactly as the real binary emits it."""
    body = {
        "ok": ok,
        "state": state,
        "has_audio": False,
        "audio_muted": False,
        **extra,
    }
    return json.dumps(body)


class FakeRunner:
    """Fakes doubletake-ctl. `status_sequence` is consumed one entry per poll."""

    def __init__(self, results=None, status_sequence=None):
        self.calls = []
        self.results = results or {}
        self.status_sequence = list(status_sequence or [])

    async def __call__(self, argv):
        self.calls.append(argv)
        if "status" in argv and self.status_sequence:
            return (0, self.status_sequence.pop(0), "")
        for key, result in self.results.items():
            if key in argv:
                return result
        return (0, envelope(), "")


def make_backend(runner, **cfg):
    states = []
    backend = AirPlayBackend(
        lambda d, s, e: states.append((s, e)),
        Config(**cfg),
        runner=runner,
        poll_interval=0.0,
    )
    return backend, states


async def test_start_launches_daemon_then_connects():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("connecting"), envelope("streaming")],
    )
    backend, states = make_backend(runner, airplay_port_range="60000-60010")
    await backend.start(make_device())

    joined = [" ".join(c) for c in runner.calls]
    assert any("-daemonize" in c and "60000-60010" in c for c in joined)
    assert any("connect 192.168.1.77" in c for c in joined)
    assert states[0][0] is SessionState.CONNECTING
    assert states[-1][0] is SessionState.STREAMING


async def test_start_polls_until_streaming():
    """connect returns 'connecting' immediately; success is only known by polling."""
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[
            envelope("connecting"),
            envelope("connecting"),
            envelope("streaming"),
        ],
    )
    backend, states = make_backend(runner)
    await backend.start(make_device())
    status_calls = [c for c in runner.calls if "status" in c]
    assert len(status_calls) == 3
    assert states[-1][0] is SessionState.STREAMING


async def test_daemon_started_only_once():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("streaming"), envelope("streaming")],
    )
    backend, _ = make_backend(runner)
    await backend.start(make_device())
    await backend.start(make_device())
    daemonize_calls = [c for c in runner.calls if "-daemonize" in c]
    assert len(daemonize_calls) == 1


async def test_pin_required_enters_awaiting_pin():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("pin_required", needs_pin=True)],
    )
    backend, states = make_backend(runner)
    await backend.start(make_device())
    assert states[-1][0] is SessionState.AWAITING_PIN


async def test_submit_pin_reaches_streaming():
    runner = FakeRunner(
        results={
            "connect": (0, envelope("connecting"), ""),
            "pin": (0, envelope("streaming"), ""),
        },
        status_sequence=[envelope("pin_required", needs_pin=True)],
    )
    backend, states = make_backend(runner)
    device = make_device()
    await backend.start(device)
    await backend.submit_pin(device, "1234")
    assert any(c[-2:] == ["pin", "1234"] for c in runner.calls)
    assert states[-1][0] is SessionState.STREAMING


async def test_connect_error_envelope_is_actionable():
    """Failure arrives as ok:false with an error string, not a failed state."""
    runner = FakeRunner(
        results={"connect": (0, envelope("idle", ok=False, error="connection timed out"), "")}
    )
    backend, states = make_backend(runner)
    with pytest.raises(BackendError, match="firewall"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED


async def test_connect_timeout_mentions_firewall():
    """Never reaching streaming is the documented silent-stall firewall case."""
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("connecting")] * 200,
    )
    backend, states = make_backend(runner)
    with pytest.raises(BackendError, match="firewall"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED


async def test_malformed_output_is_actionable():
    runner = FakeRunner(results={"connect": (0, "not json at all", "")})
    backend, _ = make_backend(runner)
    with pytest.raises(BackendError, match="unexpected output"):
        await backend.start(make_device())


async def test_stop_disconnects_specific_device():
    runner = FakeRunner(
        results={"connect": (0, envelope("connecting"), "")},
        status_sequence=[envelope("streaming")],
    )
    backend, states = make_backend(runner)
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert any("disconnect 192.168.1.77" in " ".join(c) for c in runner.calls)
    assert states[-1][0] is SessionState.IDLE


async def test_missing_doubletake_binary_is_actionable():
    async def runner(argv):
        raise FileNotFoundError(argv[0])

    backend, _ = make_backend(runner)
    with pytest.raises(BackendError, match="doubletake"):
        await backend.start(make_device())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_airplay_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.backends.airplay'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/backends/airplay.py`:

```python
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState

log = logging.getLogger(__name__)

CommandRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

DAEMON_BIN = "doubletake"
CTL_BIN = "doubletake-ctl"

# Verified against internal/daemon/daemon.go and a live AppleTV14,1.
DT_STATES = ("idle", "discovering", "connecting", "streaming", "pin_required")

STATE_MAP = {
    "idle": SessionState.IDLE,
    "discovering": SessionState.CONNECTING,
    "connecting": SessionState.CONNECTING,
    "streaming": SessionState.STREAMING,
    "pin_required": SessionState.AWAITING_PIN,
}

CONNECT_TIMEOUT = 30.0


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
    ) -> None:
        super().__init__(on_state)
        self._config = config
        self._run = runner or subprocess_runner
        self._poll_interval = poll_interval
        self._daemon_started = False

    async def _exec(self, argv: list[str]) -> tuple[int, str, str]:
        try:
            return await self._run(argv)
        except FileNotFoundError as exc:
            raise BackendError(
                f"{DAEMON_BIN} is not installed; install it with: paru -S doubletake"
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

    def _firewall_hint(self, device: Device, detail: str) -> str:
        return (
            f"could not reach {device.name}. The receiver connects back to this "
            f"machine, so inbound TCP and UDP on ports "
            f"{self._config.airplay_port_range} must be allowed — a default-DROP "
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
            message = self._firewall_hint(device, str(exc))
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message) from exc

        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            payload = await self._ctl(["status"])
            state = STATE_MAP.get(payload.get("state", ""), SessionState.CONNECTING)

            if state is SessionState.AWAITING_PIN:
                self._emit(device, SessionState.AWAITING_PIN)
                return
            if state is SessionState.STREAMING:
                self._emit(device, SessionState.STREAMING)
                return

            await asyncio.sleep(self._poll_interval)

        message = self._firewall_hint(
            device, f"never reached streaming within {CONNECT_TIMEOUT:.0f}s"
        )
        self._emit(device, SessionState.FAILED, message)
        raise BackendError(message)

    async def submit_pin(self, device: Device, pin: str) -> None:
        # doubletake-ctl pin takes only the PIN; the daemon knows the pending device.
        try:
            await self._ctl(["pin", pin])
        except BackendError as exc:
            message = f"pairing failed: {exc}"
            self._emit(device, SessionState.FAILED, message)
            raise BackendError(message) from exc
        self._emit(device, SessionState.STREAMING)

    async def stop(self, device: Device) -> None:
        self._emit(device, SessionState.STOPPING)
        await self._exec([CTL_BIN, "disconnect", device.address])
        self._emit(device, SessionState.IDLE)

    async def shutdown(self) -> None:
        if self._daemon_started:
            await self._exec([CTL_BIN, "disconnect"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_airplay_backend.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/backends/airplay.py tests/test_airplay_backend.py
git commit -m "feat: add AirPlay backend delegating to doubletake"
```

---

### Task 10: Portal ScreenCast session

**Files:**
- Create: `omarchy_cast/capture/portal.py`
- Test: `tests/test_portal.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `PortalSession(fd: int, node_id: int)` — frozen dataclass
  - `PortalError(Exception)`
  - `restore_token_path() -> Path`
  - `load_restore_token() -> str | None`, `save_restore_token(token: str) -> None`
  - `parse_streams(streams_variant) -> int` — extracts the PipeWire node id from the portal's `streams` response
  - `async def open_screencast(bus=None) -> PortalSession`

Only the pure helpers are unit-tested; `open_screencast` needs a live compositor and is exercised manually in Task 13.

- [ ] **Step 1: Write the failing test**

`tests/test_portal.py`:

```python
import pytest

from omarchy_cast.capture.portal import (
    PortalError,
    load_restore_token,
    parse_streams,
    restore_token_path,
    save_restore_token,
)


def test_parse_streams_extracts_node_id():
    streams = [(42, {"position": (0, 0), "size": (2560, 1600)})]
    assert parse_streams(streams) == 42


def test_parse_streams_takes_first_when_multiple():
    streams = [(7, {}), (9, {})]
    assert parse_streams(streams) == 7


def test_parse_streams_empty_raises():
    with pytest.raises(PortalError, match="no stream"):
        parse_streams([])


def test_restore_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert load_restore_token() is None
    save_restore_token("tok-123")
    assert load_restore_token() == "tok-123"
    assert restore_token_path().parent.exists()


def test_blank_restore_token_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save_restore_token("   ")
    assert load_restore_token() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_portal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.capture.portal'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/capture/portal.py`:

```python
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"

CURSOR_MODE_EMBEDDED = 2
SOURCE_TYPE_MONITOR = 1
PERSIST_MODE_PERSISTENT = 2


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
    token = path.read_text().strip()
    return token or None


def save_restore_token(token: str) -> None:
    path = restore_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    path.chmod(0o600)


def parse_streams(streams) -> int:
    """Portal returns a(ua{sv}) — a list of (node_id, properties) pairs."""
    for entry in streams:
        return int(entry[0])
    raise PortalError("portal returned no stream; screen capture was cancelled")


async def open_screencast(bus=None) -> PortalSession:
    """Open a ScreenCast session and return the PipeWire fd and node id.

    Requires a running compositor and xdg-desktop-portal-hyprland. Uses the
    stored restore token when present so the user is not re-prompted.
    """
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None,
        BUS_NAME, OBJECT_PATH, SCREENCAST_IFACE, None,
    )

    loop = asyncio.get_running_loop()
    pending: asyncio.Future = loop.create_future()
    results: dict = {}

    def on_response(_conn, _sender, path, _iface, _signal, params):
        code, payload = params.unpack()
        if code != 0:
            if not pending.done():
                pending.set_exception(
                    PortalError("screen capture permission denied or cancelled")
                )
            return
        results[path] = payload
        if not pending.done():
            pending.set_result(payload)

    def subscribe() -> int:
        return bus.signal_subscribe(
            BUS_NAME, "org.freedesktop.portal.Request", "Response",
            None, None, Gio.DBusSignalFlags.NONE, on_response,
        )

    async def call(method: str, args: GLib.Variant):
        nonlocal pending
        pending = loop.create_future()
        token = subscribe()
        try:
            proxy.call_sync(method, args, Gio.DBusCallFlags.NONE, -1, None)
            return await asyncio.wait_for(pending, timeout=120)
        finally:
            bus.signal_unsubscribe(token)

    payload = await call(
        "CreateSession",
        GLib.Variant("(a{sv})", ({"session_handle_token": GLib.Variant("s", "omarchycast")},)),
    )
    session_handle = payload["session_handle"]

    select_options = {
        "types": GLib.Variant("u", SOURCE_TYPE_MONITOR),
        "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", CURSOR_MODE_EMBEDDED),
        "persist_mode": GLib.Variant("u", PERSIST_MODE_PERSISTENT),
    }
    stored = load_restore_token()
    if stored:
        select_options["restore_token"] = GLib.Variant("s", stored)

    await call("SelectSources", GLib.Variant("(oa{sv})", (session_handle, select_options)))
    payload = await call("Start", GLib.Variant("(osa{sv})", (session_handle, "", {})))

    if "restore_token" in payload:
        save_restore_token(payload["restore_token"])

    node_id = parse_streams(payload["streams"])
    fd_list = proxy.call_with_unix_fd_list_sync(
        "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (session_handle, {})),
        Gio.DBusCallFlags.NONE, -1, None, None,
    )
    fd = fd_list[1].steal_fds()[0]
    return PortalSession(fd=fd, node_id=node_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_portal.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/capture/portal.py tests/test_portal.py
git commit -m "feat: add xdg-desktop-portal ScreenCast session handling"
```

---

### Task 11: GStreamer pipeline and HTTP streaming server

**Files:**
- Create: `omarchy_cast/capture/pipeline.py`
- Create: `omarchy_cast/capture/http.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_http.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `gst_element_for` (Task 3), `PortalSession` (Task 10).
- Produces:
  - `build_pipeline_description(node_id: int, fd: int, encoder: str, config: Config) -> str` — `omarchy_cast.capture.pipeline`
  - `CapturePipeline(description)` with `.start()`, `.stop()`, `.set_sink_callback(cb)`
  - `StreamServer(host: str, port: int)` — `omarchy_cast.capture.http`; `.url_path = "/stream.mkv"`, `async def start() -> int` (returns bound port), `async def stop()`, `.push(chunk: bytes)`, `.client_count`
  - `format_headers(content_type: str) -> bytes`
  - `local_address_for(target: str) -> str` — the LAN address a given receiver would reach us on

- [ ] **Step 1: Write the failing tests**

`tests/test_pipeline.py`:

```python
from omarchy_cast.capture.pipeline import build_pipeline_description
from omarchy_cast.core.config import Config


def test_pipeline_includes_node_id_and_fd():
    desc = build_pipeline_description(42, 7, "vaapi", Config())
    assert "pipewiresrc" in desc
    assert "path=42" in desc
    assert "fd=7" in desc


def test_pipeline_uses_selected_encoder_element():
    assert "vah264enc" in build_pipeline_description(1, 2, "vaapi", Config())
    assert "x264enc" in build_pipeline_description(1, 2, "x264", Config())
    assert "nvh264enc" in build_pipeline_description(1, 2, "nvenc", Config())


def test_pipeline_sets_zero_latency_for_x264():
    desc = build_pipeline_description(1, 2, "x264", Config())
    assert "tune=zerolatency" in desc


def test_pipeline_ends_in_appsink():
    desc = build_pipeline_description(1, 2, "vaapi", Config())
    assert desc.strip().endswith("name=sink")
    assert "appsink" in desc


def test_pipeline_muxes_streamable_matroska():
    desc = build_pipeline_description(1, 2, "vaapi", Config())
    assert "matroskamux" in desc
    assert "streamable=true" in desc


def test_pipeline_honours_configured_fps():
    desc = build_pipeline_description(1, 2, "vaapi", Config(fps=60))
    assert "framerate=60/1" in desc
```

`tests/test_http.py`:

```python
import asyncio

from omarchy_cast.capture.http import StreamServer, format_headers, local_address_for


def test_headers_are_chunked_and_typed():
    headers = format_headers("video/x-matroska").decode()
    assert "HTTP/1.1 200 OK" in headers
    assert "Content-Type: video/x-matroska" in headers
    assert "Transfer-Encoding: chunked" in headers
    assert headers.endswith("\r\n\r\n")


def test_local_address_for_returns_ipv4():
    address = local_address_for("192.168.1.50")
    assert address.count(".") == 3
    assert address != "0.0.0.0"


async def test_server_binds_ephemeral_port_and_reports_it():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        assert port > 0
    finally:
        await server.stop()


async def test_client_receives_pushed_chunks():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stream.mkv HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()

        header = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in header

        await asyncio.sleep(0.05)
        server.push(b"abc")

        size_line = await reader.readline()
        assert size_line.strip() == b"3"
        assert await reader.readexactly(3) == b"abc"

        writer.close()
    finally:
        await server.stop()


async def test_client_count_tracks_connections():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        assert server.client_count == 0
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stream.mkv HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        assert server.client_count == 1
        writer.close()
    finally:
        await server.stop()


async def test_unknown_path_returns_404():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        assert b"404" in await reader.readline()
        writer.close()
    finally:
        await server.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pipeline.py tests/test_http.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.capture.pipeline'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/capture/pipeline.py`:

```python
import logging

from omarchy_cast.capture.encoder import gst_element_for
from omarchy_cast.core.config import Config

log = logging.getLogger(__name__)

ENCODER_ARGS = {
    "vaapi": "rate-control=cbr target-usage=6",
    "nvenc": "preset=low-latency-hq rc-mode=cbr",
    "x264": "tune=zerolatency speed-preset=veryfast key-int-max=30",
}


def build_pipeline_description(node_id: int, fd: int, encoder: str, config: Config) -> str:
    element = gst_element_for(encoder)
    args = ENCODER_ARGS[encoder]
    return (
        f"pipewiresrc path={node_id} fd={fd} do-timestamp=true ! "
        f"videorate ! video/x-raw,framerate={config.fps}/1 ! "
        f"videoconvert ! "
        f"{element} {args} ! "
        f"h264parse config-interval=1 ! "
        f"matroskamux streamable=true ! "
        f"appsink emit-signals=true sync=false max-buffers=4 drop=true name=sink"
    )


class CapturePipeline:
    """Wraps a GStreamer pipeline whose appsink hands buffers to a callback."""

    def __init__(self, description: str) -> None:
        self.description = description
        self._pipeline = None
        self._callback = None

    def set_sink_callback(self, callback) -> None:
        self._callback = callback

    def _on_sample(self, sink) -> int:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                if self._callback is not None:
                    self._callback(bytes(info.data))
            finally:
                buffer.unmap(info)
        return Gst.FlowReturn.OK

    def start(self) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self._pipeline = Gst.parse_launch(self.description)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        log.info("pipeline started: %s", self.description)

    def stop(self) -> None:
        if self._pipeline is None:
            return
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
```

`omarchy_cast/capture/http.py`:

```python
import asyncio
import contextlib
import logging
import socket

log = logging.getLogger(__name__)

STREAM_PATH = "/stream.mkv"
CONTENT_TYPE = "video/x-matroska"


def format_headers(content_type: str) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Cache-Control: no-cache, no-store\r\n"
        "Connection: close\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "\r\n"
    ).encode("ascii")


def local_address_for(target: str) -> str:
    """The local IPv4 address the kernel would use to reach `target`.

    Uses a connectionless UDP socket, so nothing is actually sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


class StreamServer:
    """Single-endpoint HTTP server that pushes live chunks to connected clients."""

    url_path = STREAM_PATH

    def __init__(self, host: str, port: int = 0) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def client_count(self) -> int:
        return len(self._writers)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def push(self, chunk: bytes) -> None:
        if not chunk:
            return
        framed = b"%x\r\n%s\r\n" % (len(chunk), chunk)
        for writer in list(self._writers):
            if writer.is_closing():
                self._writers.discard(writer)
                continue
            try:
                writer.write(framed)
            except Exception:
                self._writers.discard(writer)

    async def _on_client(self, reader, writer) -> None:
        try:
            request_line = await reader.readline()
            while True:
                header = await reader.readline()
                if header in (b"\r\n", b"\n", b""):
                    break

            if STREAM_PATH.encode() not in request_line:
                writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            writer.write(format_headers(CONTENT_TYPE))
            await writer.drain()
            self._writers.add(writer)

            while not writer.is_closing():
                await asyncio.sleep(0.2)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pipeline.py tests/test_http.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add omarchy_cast/capture/pipeline.py omarchy_cast/capture/http.py tests/test_pipeline.py tests/test_http.py
git commit -m "feat: add GStreamer capture pipeline and HTTP stream server"
```

---

### Task 12: Cast backend and daemon wiring

**Files:**
- Create: `omarchy_cast/backends/cast.py`
- Modify: `omarchy_cast/core/daemon.py` (replace the `main()` stub)
- Test: `tests/test_cast_backend.py`

**Interfaces:**
- Consumes: `Backend` (Task 5), `Config` (Task 1), `select_encoder`/`probe_available` (Task 3), `open_screencast` (Task 10), `build_pipeline_description`/`CapturePipeline`/`StreamServer`/`local_address_for` (Task 11).
- Produces:
  - `CAST_APP_ID = "CC1AD845"`
  - `CastBackend(on_state, config, *, capture_factory=None, chromecast_factory=None)` with `protocol = "cast"`
  - `main()` in `daemon.py` — constructs `Discovery`, both backends, and serves

The two factory arguments exist so tests can inject fakes; production defaults build the real pipeline and a real `pychromecast.Chromecast`.

- [ ] **Step 1: Write the failing test**

`tests/test_cast_backend.py`:

```python
import pytest

from omarchy_cast.backends.base import BackendError
from omarchy_cast.backends.cast import CAST_APP_ID, CastBackend
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


def make_device():
    return Device(id="cast:1", name="Bedroom", address="192.168.1.50", port=8009, protocol="cast")


class FakeCapture:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.url = None

    async def start(self, device):
        self.started = True
        self.url = f"http://192.168.1.10:9999/stream.mkv"
        return self.url

    async def stop(self):
        self.stopped = True


class FakeMediaController:
    def __init__(self, fail=False):
        self.played = None
        self._fail = fail

    def play_media(self, url, content_type, stream_type=None):
        if self._fail:
            raise RuntimeError("receiver refused")
        self.played = (url, content_type, stream_type)

    def block_until_active(self, timeout=None):
        return None

    def stop(self):
        self.played = None


class FakeChromecast:
    def __init__(self, fail_connect=False, fail_play=False):
        self.app_id = None
        self.media_controller = FakeMediaController(fail=fail_play)
        self.disconnected = False
        self._fail_connect = fail_connect

    def wait(self, timeout=None):
        if self._fail_connect:
            raise OSError("unreachable")

    def start_app(self, app_id):
        self.app_id = app_id

    def quit_app(self):
        self.app_id = None

    def disconnect(self):
        self.disconnected = True


def make_backend(capture=None, cast=None):
    states = []
    capture = capture or FakeCapture()
    cast = cast or FakeChromecast()
    backend = CastBackend(
        lambda d, s, e: states.append((s, e)),
        Config(),
        capture_factory=lambda cfg: capture,
        chromecast_factory=lambda device: cast,
    )
    return backend, states, capture, cast


async def test_start_launches_default_media_receiver():
    backend, states, capture, cast = make_backend()
    await backend.start(make_device())
    assert cast.app_id == CAST_APP_ID
    assert capture.started is True
    assert states[0][0] is SessionState.CONNECTING
    assert states[-1][0] is SessionState.STREAMING


async def test_start_loads_stream_url_as_live_matroska():
    backend, _, capture, cast = make_backend()
    await backend.start(make_device())
    url, content_type, stream_type = cast.media_controller.played
    assert url == capture.url
    assert content_type == "video/x-matroska"
    assert stream_type == "LIVE"


async def test_unreachable_device_is_actionable():
    backend, states, capture, _ = make_backend(cast=FakeChromecast(fail_connect=True))
    with pytest.raises(BackendError, match="could not connect"):
        await backend.start(make_device())
    assert states[-1][0] is SessionState.FAILED
    assert capture.stopped is True


async def test_receiver_refusing_media_stops_capture():
    backend, states, capture, _ = make_backend(cast=FakeChromecast(fail_play=True))
    with pytest.raises(BackendError):
        await backend.start(make_device())
    assert capture.stopped is True
    assert states[-1][0] is SessionState.FAILED


async def test_stop_quits_app_and_stops_capture():
    backend, states, capture, cast = make_backend()
    device = make_device()
    await backend.start(device)
    await backend.stop(device)
    assert cast.app_id is None
    assert capture.stopped is True
    assert cast.disconnected is True
    assert states[-1][0] is SessionState.IDLE


async def test_cast_does_not_support_pin():
    backend, _, _, _ = make_backend()
    with pytest.raises(BackendError, match="PIN"):
        await backend.submit_pin(make_device(), "1234")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cast_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'omarchy_cast.backends.cast'`

- [ ] **Step 3: Write minimal implementation**

`omarchy_cast/backends/cast.py`:

```python
import asyncio
import logging

from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.capture.encoder import NoEncoderAvailable, probe_available, select_encoder
from omarchy_cast.capture.http import CONTENT_TYPE, StreamServer, local_address_for
from omarchy_cast.capture.pipeline import CapturePipeline, build_pipeline_description
from omarchy_cast.capture.portal import PortalError, open_screencast
from omarchy_cast.core.config import Config
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState

log = logging.getLogger(__name__)

CAST_APP_ID = "CC1AD845"
CONNECT_TIMEOUT = 10.0


class CaptureService:
    """Owns the portal session, the GStreamer pipeline, and the HTTP server."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._pipeline: CapturePipeline | None = None
        self._server: StreamServer | None = None

    async def start(self, device: Device) -> str:
        try:
            encoder = select_encoder(self._config, probe_available())
        except NoEncoderAvailable as exc:
            raise BackendError(str(exc)) from exc

        try:
            portal = await open_screencast()
        except PortalError as exc:
            raise BackendError(str(exc)) from exc

        host = local_address_for(device.address)
        self._server = StreamServer(host, self._config.cast_http_port)
        port = await self._server.start()

        description = build_pipeline_description(
            portal.node_id, portal.fd, encoder, self._config
        )
        self._pipeline = CapturePipeline(description)
        loop = asyncio.get_running_loop()
        self._pipeline.set_sink_callback(
            lambda chunk: loop.call_soon_threadsafe(self._server.push, chunk)
        )
        self._pipeline.start()
        return f"http://{host}:{port}{StreamServer.url_path}"

    async def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
        if self._server is not None:
            await self._server.stop()
            self._server = None


def _default_chromecast_factory(device: Device):
    import pychromecast

    return pychromecast.Chromecast(pychromecast.CastInfo(
        services=set(), uuid=None, model_name=device.model,
        friendly_name=device.name, host=device.address, port=device.port,
        cast_type="cast", manufacturer="",
    ))


class CastBackend(Backend):
    """Casts to a Chromecast via the Default Media Receiver.

    Latency is 1-3 seconds because the receiver buffers a media stream. This is
    not the low-latency Chrome Mirroring path; see the design doc for why.
    """

    protocol = "cast"

    def __init__(
        self,
        on_state: StateCallback,
        config: Config,
        *,
        capture_factory=None,
        chromecast_factory=None,
    ) -> None:
        super().__init__(on_state)
        self._config = config
        self._capture_factory = capture_factory or CaptureService
        self._chromecast_factory = chromecast_factory or _default_chromecast_factory
        self._capture = None
        self._cast = None

    async def start(self, device: Device) -> None:
        self._emit(device, SessionState.CONNECTING)
        self._capture = self._capture_factory(self._config)

        try:
            url = await self._capture.start(device)
        except BackendError as exc:
            self._emit(device, SessionState.FAILED, str(exc))
            raise

        try:
            self._cast = self._chromecast_factory(device)
            await asyncio.to_thread(self._cast.wait, CONNECT_TIMEOUT)
            self._cast.start_app(CAST_APP_ID)
            self._cast.media_controller.play_media(url, CONTENT_TYPE, stream_type="LIVE")
        except OSError as exc:
            message = (
                f"could not connect to {device.name} at {device.address}: {exc}"
            )
            await self._fail(device, message)
            raise BackendError(message) from exc
        except Exception as exc:
            message = (
                f"{device.name} refused the stream: {exc}. The receiver must be able "
                f"to reach {url} — check that no firewall blocks that port."
            )
            await self._fail(device, message)
            raise BackendError(message) from exc

        self._emit(device, SessionState.STREAMING)

    async def _fail(self, device: Device, message: str) -> None:
        await self._teardown()
        self._emit(device, SessionState.FAILED, message)

    async def _teardown(self) -> None:
        if self._capture is not None:
            await self._capture.stop()
            self._capture = None
        if self._cast is not None:
            try:
                self._cast.quit_app()
                self._cast.disconnect()
            except Exception:
                log.debug("error while disconnecting cast device", exc_info=True)
            self._cast = None

    async def stop(self, device: Device) -> None:
        self._emit(device, SessionState.STOPPING)
        await self._teardown()
        self._emit(device, SessionState.IDLE)

    async def shutdown(self) -> None:
        await self._teardown()
```

- [ ] **Step 4: Replace the daemon `main()` stub**

In `omarchy_cast/core/daemon.py`, replace the `main()` function with:

```python
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
    discovery = Discovery()
    daemon = Daemon(discovery, {}, idle_timeout=args.idle_timeout)
    daemon.backends["airplay"] = AirPlayBackend(daemon.on_state, config)
    daemon.backends["cast"] = CastBackend(daemon.on_state, config)

    asyncio.run(daemon.serve())


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_cast_backend.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the whole suite**

Run: `pytest -v`
Expected: PASS, all tests green

- [ ] **Step 7: Commit**

```bash
git add omarchy_cast/backends/cast.py omarchy_cast/core/daemon.py tests/test_cast_backend.py
git commit -m "feat: add Cast backend and wire up the daemon"
```

---

### Task 13: Packaging, docs, and manual verification

**Files:**
- Create: `README.md`
- Create: `install.sh`
- Create: `uninstall.sh`
- Create: `PKGBUILD`
- Create: `docs/device-matrix.md`

**Interfaces:**
- Consumes: everything.
- Produces: an installable package.

- [ ] **Step 1: Write the README**

`README.md` must contain, at minimum:

- One-line description and an install section (`paru -S omarchy-cast`).
- A **Latency** section stating plainly: *AirPlay mirroring targets ~100ms. Chromecast casting runs 1–3 seconds behind because the Default Media Receiver buffers a media stream — it is good for video and presentations, and not usable as a second display.* This is a spec requirement, not optional copy.
- A **Firewall** section reproducing doubletake's requirement: the Apple TV connects back to the sender, so inbound TCP and UDP on the configured `port_range` (default `60000-60010`) must be allowed, with a `ufw` example.
- Setup for the waybar module, pointing at `share/waybar/cast-indicator.jsonc`.
- A keybind example for `~/.config/hypr/bindings.conf`:
  ```
  bindd = SUPER ALT, C, Cast screen, exec, omarchy-cast menu
  ```
- A link to `docs/device-matrix.md`.

- [ ] **Step 2: Write the device matrix**

`docs/device-matrix.md`: a table with columns `Device | Protocol | Status | Notes`, seeded with the devices the author has tested and an invitation to open an issue with results. Start it with whatever is verified in Step 6 below — do not invent entries.

- [ ] **Step 3: Write `install.sh` and `uninstall.sh`**

Follow the `~/workspace/omarchy-prayer/install.sh` pattern. `install.sh` must:
1. Check for `doubletake` and print the `paru -S doubletake` hint if missing, without failing the install.
2. `pip install --user .` or install to `~/.local`.
3. Print the waybar and keybind snippets to add, rather than editing the user's configs in place.

- [ ] **Step 4: Write the PKGBUILD**

```bash
pkgname=omarchy-cast
pkgver=0.1.0
pkgrel=1
pkgdesc="Desktop mirroring for Omarchy to AirPlay and Google Cast receivers"
arch=('any')
url="https://github.com/mrCode/omarchy-cast"
license=('MIT')
depends=(
  'python' 'python-pychromecast' 'python-zeroconf' 'python-gobject'
  'gst-python' 'gst-plugins-base' 'gst-plugins-good' 'gst-plugins-bad'
  'gst-plugin-va' 'xdg-desktop-portal' 'pipewire'
)
optdepends=('doubletake: AirPlay mirroring support')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
  cd "$pkgname-$pkgver"
  python -m build --wheel --no-isolation
}

package() {
  cd "$pkgname-$pkgver"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 share/waybar/cast-indicator.jsonc \
    "$pkgdir/usr/share/$pkgname/waybar/cast-indicator.jsonc"
}
```

Replace `sha256sums=('SKIP')` with the real checksum once a tag exists.

- [ ] **Step 5: Verify the full test suite passes**

Run: `pytest -v`
Expected: PASS, all tests green. Record the count.

- [ ] **Step 6: Manual verification against real hardware**

These cannot be automated. Run each and record the result in `docs/device-matrix.md`:

1. `omarchy-cast list` — both an Apple TV and a Chromecast appear.
2. `omarchy-cast start <airplay-id>` — portal prompts once, mirroring appears on the Apple TV.
3. `omarchy-cast status` — reports `streaming`.
4. `omarchy-cast waybar` — emits JSON with `"class": "streaming"`.
5. `omarchy-cast stop` — mirroring ends, daemon exits after ~30s (`pgrep -f omarchy-castd` empty).
6. `omarchy-cast start <cast-id>` — desktop appears on the Chromecast. Note the observed latency.
7. `omarchy-cast menu` — walker lists both devices; selecting one starts a cast.
8. Start a second cast while one is active — confirm both run and waybar shows `2`.
9. Kill `doubletake` mid-session — confirm the session reports FAILED and waybar returns to idle rather than hanging.

If any step fails, fix it before proceeding. Do not mark this task complete with a failing step.

- [ ] **Step 7: Commit**

```bash
git add README.md install.sh uninstall.sh PKGBUILD docs/device-matrix.md
git commit -m "docs: add README, packaging, and device matrix"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: architecture and process lifecycle → Tasks 6, 7; module layout → Tasks 1–12; shared discovery → Task 4; encoder selection with NVENC-last ranking → Task 3; AirPlay data flow including the PIN path → Task 9; Cast data flow including `CC1AD845` and live matroska → Tasks 11, 12; session states → Task 2; all seven error-handling rows → Tasks 9 (firewall, missing binary), 10 (portal denial), 12 (unreachable receiver, no encoder), 6 (session cleanup); testing strategy → every task; packaging with the verified dependency list → Task 13; the README latency disclosure risk → Task 13 Step 1.

**Two spec items intentionally deferred, both already marked post-MVP in the spec:** the TUI, and the mako notification on mid-stream doubletake exit. The latter is partially covered — `_run_menu` sends `notify-send` on start failure — but supervising an already-running doubletake process for unexpected exit needs a poll loop that is not in this plan. It should be the first task of the follow-up plan.

**Placeholder scan.** No TBD/TODO markers. Every code step carries real code. The one `sha256sums=('SKIP')` is explicitly annotated as requiring a real value at tag time.

**Type consistency.** `Device`, `SessionState`, `Session`, `Backend`, `StateCallback`, `BackendError`, `Config`, `request()`, `render()`, `format_entries()`/`parse_selection()`, `build_pipeline_description()`, `StreamServer`, `PortalSession` all keep identical signatures across the tasks that define and consume them. `CONTENT_TYPE` is defined once in `capture/http.py` and imported by `backends/cast.py` rather than redeclared.
