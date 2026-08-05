# omarchy-cast — Design

**Date:** 2026-08-05
**Status:** Approved, pending implementation plan

## Summary

`omarchy-cast` mirrors the Omarchy desktop (Hyprland/Wayland) to AirPlay and
Google Cast receivers. A daemon owns discovery, session state, and the Cast
media pipeline. Thin clients — a CLI, a walker menu, a waybar indicator, and a
TUI — drive it over a Unix socket.

AirPlay is delegated to [doubletake](https://github.com/omarroth/doubletake),
which already implements the protocol. Cast is implemented here, using
[pychromecast](https://github.com/home-assistant-libs/pychromecast) for the
CastV2 channel and GStreamer for capture and encoding.

## Goals

- Mirror the desktop to an Apple TV with one keybind.
- Mirror the desktop to a Chromecast with the same keybind and the same UI.
- Show cast state in waybar; stop a cast from there.
- Ship as an AUR package with an `install.sh`, matching `omarchy-prayer`.

## Non-goals

These are deliberately excluded from v1. Each is a separate design cycle.

- DLNA/UPnP-AV. Renderers reject unknown-duration live streams; latency is
  multiple seconds. Cheap to add later as a fallback for old TVs.
- Miracast. The source side exists only in gnome-network-displays, and the
  Wi-Fi Direct layer (wpa_supplicant P2P + NetworkManager) is the most
  failure-prone component surveyed. MICE would be the entry point if revisited.
- True Cast mirroring via the Chrome Mirroring app (`0F5096E8`). Blocked
  upstream on AES-CTR-128; solvable via openscreen's `FrameCrypto`, but that is
  original protocol work, not app work.
- Per-sink bitrate adaptation, virtual-display casting, HDR.

## Background

Research (2026-08-05) established the constraints this design works within:

- FOSS AirPlay was receiver-only until doubletake appeared in April 2026.
  doubletake is a genuine sender: FairPlay SAP, SRP-6a pairing, PipeWire
  capture, VA-API/NVENC encoding, daemon mode with a control socket. It is
  LGPL-3.0 and packaged in the AUR.
- No AirPlay hardware auth chip is required for a sender. MFi (`/auth-setup`)
  applies to accessory receivers; senders use FairPlay SAP (`/fp-setup`), which
  is software-only.
- Cast device authentication runs sender→receiver only. No Google-issued
  certificate is needed to *be* a sender.
- Cast has two protocols behind one name. The Chrome Mirroring app gives real
  low-latency mirroring but requires AES-CTR-128 that GStreamer/libsrtp lack.
  The Default Media Receiver (`CC1AD845`) plays a live HTTP stream — higher
  latency, no crypto work. gnome-network-displays ships the latter; this design
  follows it.

## Architecture

Approach chosen: **thin orchestrator**. The daemon delegates AirPlay entirely
and implements only Cast.

```
  walker menu ─┐
  CLI ─────────┼──▶ omarchy-cast.sock ──▶ omarchy-castd
  TUI ─────────┤                             │
  waybar ──────┘                             ├─▶ AirPlayBackend ──▶ doubletake -daemonize
                                             │                       (supervised, via doubletake-ctl)
                                             └─▶ CastBackend ─────▶ pychromecast (CastV2)
                                                                  + GStreamer ─▶ local HTTP server
```

### Why not a unified capture core

A single portal session feeding one encode out to all sinks is the better
architecture on paper. It is rejected for v1 because doubletake accepts no
external frame source and exposes no output-selection flag — it captures for
itself. Unifying capture therefore requires either patching doubletake upstream
or reimplementing AirPlay mirroring (FairPlay SAP, SRP-6a, ChaCha20-Poly1305).
That is months of protocol work to optimize a case — casting to an Apple TV and
a Chromecast simultaneously — that is rare.

The cost is accepted explicitly: **casting to both protocols at once means two
portal sessions and two encodes.**

The `Backend` interface is defined so the daemon cannot tell whether a backend
brings its own capture or consumes a shared frame source. If doubletake gains an
input flag, unified capture becomes a backend swap rather than a rewrite.

### Process lifecycle

The daemon is not always-running. Any client spawns it if the socket is dead,
and it exits after 30 seconds with no active session. waybar renders idle when
the socket is absent. Nothing runs while not casting.

### Module layout

```
omarchy_cast/
  core/
    daemon.py      — asyncio event loop, socket API, session registry, notifier
    session.py     — Session: device + backend + state + stats
    discovery.py   — one zeroconf instance, two browsers, normalized Device
    config.py      — TOML at ~/.config/omarchy-cast/config.toml
  backends/
    base.py        — Backend ABC: discover / start / stop / status
    airplay.py     — doubletake process supervision + ctl socket client
    cast.py        — pychromecast + pipeline + HTTP server
  capture/
    pipeline.py    — pipewiresrc → encode → mux → multisocketsink
    encoder.py     — probe and rank available encoders, cache result
  cli/
    main.py        — argparse CLI
    menu.py        — walker --dmenu integration
    waybar.py      — JSON status output
  tui/
    app.py         — Textual client
    model.py       — presentation logic, tested without a terminal
```

### Shared discovery

Both protocols are mDNS: `_airplay._tcp.local.` and `_raop._tcp.local.` for
AirPlay, `_googlecast._tcp.local.` for Cast. One `Zeroconf` instance drives two
`ServiceBrowser`s and emits a single normalized `Device` record carrying id,
display name, address, protocol, and model. pychromecast accepts an externally
supplied zeroconf instance, so this composes rather than conflicts.

This is the component that makes the result one application rather than two
tools behind one command.

### Encoder selection

Probed once at first use and cached in the config directory. Default ranking:

1. VA-API (`vah264enc`) — Intel iGPU
2. x264 (`x264enc`, Zero Latency preset)
3. NVENC

NVENC is ranked last deliberately, inverting the usual advice. On the target
hardware (ASUS ROG Zephyrus G16 GU605CR) the display pipeline runs off the Intel
iGPU, so NVENC forces a cross-GPU copy, and waking the dGPU per frame is the
workload `NVreg_DynamicPowerManagement=0` exists to avoid. Users on dGPU-primary
systems can override the ranking in config.

## Data flow

### AirPlay session start

1. Client sends `start(device_id)` over the socket.
2. `AirPlayBackend` ensures `doubletake -daemonize` is running, with
   `-port-range` pinned to a configured window.
3. Backend issues `doubletake-ctl connect <ip>`.
4. If pairing is required, the Apple TV displays a PIN. The session enters
   `AWAITING_PIN`; the walker front-end prompts via `omarchy-menu-input` and the
   daemon relays `doubletake-ctl pin <PIN>`.
5. The portal prompts for output selection on first run. The restore token is
   persisted so subsequent sessions do not re-prompt.
6. Session becomes `STREAMING`. waybar updates.

### Cast session start

1. Client sends `start(device_id)`.
2. `CastBackend` builds `pipewiresrc → vah264enc → matroskamux →
   multisocketsink` and binds an HTTP server to the LAN-facing address.
3. pychromecast connects, launches `CC1AD845`, and issues LOAD with
   `contentType: video/x-matroska` as a live stream.
4. Session becomes `STREAMING` when the receiver reports PLAYING.

### Session states

`IDLE → CONNECTING → [AWAITING_PIN] → STREAMING → STOPPING → IDLE`,
with `FAILED` reachable from any state and carrying an actionable message.

## Error handling

Every entry below is a known failure mode, not a hypothetical. Each must produce
an actionable message, never an unhandled traceback.

| Failure | Handling |
|---|---|
| Portal prompt denied or cancelled | Report "screen capture permission denied". Session never enters STREAMING. |
| AirPlay reverse handshake blocked by firewall | doubletake stalls silently and forever when the receiver cannot connect back. Pin `-port-range`, apply a SETUP timeout, and report the blocked range with the exact firewall rule to add. |
| Chromecast cannot reach the HTTP server | Bind to the resolved LAN interface rather than `0.0.0.0` unconditionally. Time out waiting for PLAYING and report unreachability. |
| doubletake exits mid-stream | Supervisor detects exit, marks the session FAILED, sends a mako notification, reaps the process, returns waybar to idle. |
| Another sender takes the Chromecast | pychromecast status callback ends the session cleanly. |
| No hardware encoder available | Fall back to x264 zero-latency, warn once, do not fail. |
| Device disappears from mDNS mid-session | Session FAILED with "device went offline". |
| doubletake dies mid-stream | A per-session supervisor polls `status`; on an unexpected drop the session goes FAILED and a mako notification fires. AWAITING_PIN is exempt, since waiting on a PIN can last minutes. |

## Testing

Unit tests cover the components with real logic: discovery normalization,
encoder probing and ranking, config load/merge/validate, and the session state
machine. The daemon socket protocol is tested against a stub backend, so client
behaviour — CLI, waybar JSON, menu output — is verifiable without hardware.

Protocol correctness against real receivers is not unit-testable. It gets a
documented device matrix in the README, following doubletake's precedent, listing
confirmed-working and known-broken devices.

Test layout and tooling follow `omarchy-prayer`.

## Packaging

All Python dependencies are official Arch packages, verified 2026-08-05:

| Package | Repo | Version |
|---|---|---|
| `python-pychromecast` | extra | 14.0.10-1 |
| `python-zeroconf` | extra | 0.149.1-1 |
| `python-gobject` | extra | 3.56.3-1 |
| `gst-plugin-va` | extra | 1.28.5-2 |
| `gst-plugins-bad` | extra | 1.28.5-2 |
| `python-textual` | extra | 8.2.8-1 |

`doubletake` is the only AUR dependency. An `install.sh` and AUR PKGBUILD follow
the `omarchy-prayer` pattern, with a separate `aur-omarchy-cast` workspace.

## Delivery order

**MVP (done):** discovery, both backends, CLI, walker menu, waybar indicator,
config.

**Follow-up (done):** the TUI, and supervision of live streams so a mid-session
doubletake crash is detected rather than leaving a session STREAMING forever.

**Deferred:** AUR packaging. The PKGBUILD exists but publishing waits until the
Cast backend has been exercised against real hardware and upstream doubletake
#26 lands, since shipping a package whose AirPlay path fails on any
password-protected receiver would generate bug reports we cannot act on.

Reviewed after the result is in real use, before deciding on v2 scope.

## Risks

- **doubletake is four months old and LLM-authored by its author's own
  statement.** Depend on it via subprocess and control socket only; never vendor
  its internals. Open issues include mirroring sending a single frame (#21) and
  latency complaints (#6). Budget for it becoming unmaintained; the `Backend`
  ABC is the insulation.
- **tvOS updates can break AirPlay at any time.** Unhedgeable. Inherited from
  doubletake rather than owned here, which is the point of delegating.
- **Cast is undocumented and Google-controlled.** The March 2025 expired-CA
  incident broke Chromecast authentication device-wide; since senders validate
  the receiver chain, Google's PKI problems surface as our bug reports.
- **Cast latency via the Default Media Receiver is 1–3 seconds.** Adequate for
  video and presentations, not for use as a second display. Must be stated
  plainly in the README so expectations are set before install.
- **Dual-protocol simultaneous casting costs two encodes.** Accepted; documented.

## References

- doubletake — https://github.com/omarroth/doubletake
- pychromecast — https://github.com/home-assistant-libs/pychromecast
- gnome-network-displays — https://gitlab.gnome.org/GNOME/gnome-network-displays
- Cast support GSoC report — https://kyteinsky.github.io/p/gsoc-finale/
- openscreen — https://github.com/chromium/openscreen
- AirPlay 2 Internals — https://emanuelecozzi.net/docs/airplay2/authentication/
- Chromecast device authentication — https://tristanpenman.com/blog/posts/2025/03/22/chromecast-device-authentication/
