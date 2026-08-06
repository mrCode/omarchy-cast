# omarchy-cast

Mirror your Omarchy desktop to an Apple TV, from a walker menu with a waybar
indicator.

Built for Hyprland/Wayland. Screen capture goes through xdg-desktop-portal and
PipeWire, encoded on the GPU.

**Status: v0.1.** AirPlay works and is verified on real hardware. Chromecast
support is written but **has never been tested against a real device** — see
[Chromecast](#chromecast) before relying on it.

## What works

| Target | Status | Latency |
|---|---|---|
| **AirPlay** (Apple TV) | Verified on an `AppleTV11,1` at a sustained 31 fps | ~100–300 ms |
| **Chromecast** | **Untested against hardware.** Unit-tested against fakes only | ~1–3 s by design |

### Chromecast

The code is here and it may work, but no Chromecast has ever been on a network
where this was tested. Starting a Cast session prints a warning saying so. If
you try it, please [open an issue](https://github.com/mrCode/omarchy-cast/issues)
with the result — a confirmed failure is as useful as a success.

Even once it works, Cast goes through the Default Media Receiver, which buffers
a media stream. **It will not be usable as a second display** — expect your
cursor to lag a second or more behind. That is the protocol path, not a bug.

Real low-latency Cast mirroring needs the Chrome Mirroring receiver app, which
requires AES-CTR-128 that GStreamer's SRTP elements do not implement. See
`docs/superpowers/specs/` for the reasoning.

## Install

```bash
git clone https://github.com/mrCode/omarchy-cast && cd omarchy-cast && ./install.sh
```

This copies the package to `~/.local/share/omarchy-cast` and writes launchers
into `~/.local/bin`. It deliberately does not use pip: Arch ships `python`
without pip and marks the environment externally-managed (PEP 668), and every
dependency is a system package already.

AirPlay support additionally needs [doubletake](https://github.com/omarroth/doubletake)
— specifically the **git** package:

```bash
yay -S doubletake-git
```

The 0.4.0 release cannot capture on Hyprland: it hardcodes `vapostproc`, which
fails to import the compositor's padded DMA-BUF and mirrors a black screen with
no error. `doubletake-git` has a software fallback that omarchy-cast triggers.

Uninstall with `./uninstall.sh`. An AUR `PKGBUILD` is included but not yet
published.

## Usage

```bash
omarchy-cast list
```

```bash
omarchy-cast start airplay:AA:BB:CC:DD:EE:FF
```

```bash
omarchy-cast stop
```

`omarchy-cast menu` opens a walker picker of discovered receivers and starts the
one you choose. `omarchy-cast status` shows what is running.

### TUI

```bash
omarchy-cast-tui
```

A full-screen view of every receiver and its live state, refreshed every two
seconds. `Enter` starts the highlighted device, `s` stops, `a` adds one by
address, `r` refreshes, `q` quits. PIN prompts appear inline when a receiver
asks for one.

It is just another client of the same daemon, so the TUI, the CLI, the menu and
the waybar indicator always agree.

### Extend mode

`--mode extend` casts a virtual 1920x1080 output instead of the screen. It
shows up in Hyprland like any other monitor — drag a window onto it and that
window appears on the receiver. The output disappears again as soon as the
cast stops.

```bash
omarchy-cast start <id> --mode extend
```

The walker menu asks "Mirror or extend?" as a second prompt once you pick a
receiver, and the TUI binds `e` to it directly (`Enter` still starts a normal
mirror).

Unlike mirror, extend does **not** change your display resolution. Mirror has
to switch the panel to 1920x1080 because doubletake negotiates the stream at
1080p and its capture path has no scaler (see [Known limitations](#known-limitations)
below); a virtual output is already 1080p, so extend has nothing to switch.

**The first-run portal prompt matters: choose the `omarchy-cast` output.**
Picking the laptop panel instead silently produces a mirror, not an extend,
and gives no error. Worse, that choice is stored as a portal restore token, so
the wrong pick repeats on every extend afterward with no further prompt to
catch it. If that happens, delete the stored token to force a fresh prompt:

```bash
rm ~/.local/state/omarchy-cast/doubletake-extend-credentials.json
```

Only one extend session runs at a time — starting a second is rejected with
an `already extending to <device>; stop it first` error. This limit is
specific to extend; mirroring to several receivers at once still works as
before.

### When discovery finds nothing

mDNS is not reliable. Some access points do not forward multicast between
clients, so a receiver can be fully reachable and still invisible to `list`.
Connect by address instead:

```bash
omarchy-cast start --address 192.168.1.231
```

Use `--protocol cast` for a Chromecast. The walker menu offers the same thing as
its last entry.

### Pairing

An Apple TV shows a 4-digit PIN the first time you connect:

```bash
omarchy-cast pin airplay:AA:BB:CC:DD:EE:FF 4029
```

Credentials are saved by doubletake, so this is a one-time step per device.

## Firewall

**AirPlay will hang forever without this.** The receiver connects *back* to your
machine, so inbound traffic on the configured port range must be allowed. With a
default-DROP firewall, the connection stalls silently with no error.

```bash
sudo ufw allow proto tcp from 192.168.1.0/24 to any port 60000:60010
```

```bash
sudo ufw allow proto udp from 192.168.1.0/24 to any port 60000:60010
```

Chromecast needs one too. The receiver fetches the video *from* this machine
over HTTP, so the stream port must be reachable:

```bash
sudo ufw allow proto tcp from 192.168.1.0/24 to any port 8010
```

Replace `192.168.1.0/24` with your own subnet. Scope these to the receiver's IP
instead if you prefer a tighter rule — but remember DHCP can move it.

`8010` is `cast.http_port` from the config. It is a fixed port on purpose: an
ephemeral one lands somewhere in 32768–60999 and cannot be allowed through a
firewall in advance, which breaks casting in a way that looks like the receiver
rejecting the stream.

## Waybar

Add `"custom/cast-indicator"` to `modules-right` in
`~/.config/waybar/config.jsonc`, then merge in the block from
[share/waybar/cast-indicator.jsonc](share/waybar/cast-indicator.jsonc) and
append [share/waybar/cast-indicator.css](share/waybar/cast-indicator.css) to
your `style.css`.

The icon stays visible in every state and colour-codes it: dim when idle, yellow
connecting, green streaming, red failed. Left-click opens the menu, right-click
stops.

## Keybinding

In `~/.config/hypr/bindings.conf`:

```
bindd = SUPER ALT, C, Cast screen, exec, omarchy-cast menu
```

## Configuration

`~/.config/omarchy-cast/config.toml`, all keys optional:

```toml
[capture]
fps = 30
encoder = "auto"                          # auto | vaapi | nvenc | x264
encoder_ranking = ["vaapi", "x264", "nvenc"]

[airplay]
port_range = "60000-60010"
bitrate = 0                               # kbps, 0 = auto
code = ""                                 # reserved; see doubletake#26
hide_vapostproc = true                    # required on Hyprland; see below
auto_resolution = true                    # switch to 1080p while casting

[cast]
bitrate = 8000                            # kbps
http_port = 8010                          # fixed so it can be firewalled
```

NVENC is ranked **last** on purpose. On laptops whose display runs off the
integrated GPU, encoding on the discrete GPU forces a cross-GPU copy of every
frame, which costs more latency than the faster encoder saves. Override
`encoder_ranking` if your display is driven by the dGPU.

## Known limitations

**Do not run doubletake in daemon mode.** Its `daemon.Config` has no port
fields, so `-port-range` is silently dropped and the receiver's reverse
handshake lands on random ephemeral ports that a firewall discards. omarchy-cast
therefore runs `doubletake -target` directly, one child process per session,
where the flag is honoured.

If you drive doubletake yourself, use `-target`, not `-daemonize`.

This is **not** a tvOS authentication problem. Pairing with an onscreen code,
credential persistence and `pair-verify` all work; an Apple TV with only an
onscreen code and no AirPlay password mirrors fine.

**AirPlay switches your display to 1920x1080 while casting.** doubletake
negotiates the stream at 1080p and its capture path has no scaler, so a
higher-resolution display makes the receiver drop the connection. omarchy-cast
therefore switches the focused monitor when a cast starts and restores it when
the cast stops, fails, or the daemon exits — including after a crash, since the
previous mode is written to disk before switching.

Disable with `auto_resolution = false` under `[airplay]` if you would rather
manage it yourself. Tracked upstream as
[doubletake#28](https://github.com/omarroth/doubletake/issues/28); once that
lands this can go away.

**Casting to both protocols at once costs two encodes.** AirPlay capture is
owned by doubletake and cannot be shared, so a simultaneous AirPlay + Chromecast
session runs two portal sessions and two encoders.

See [docs/device-matrix.md](docs/device-matrix.md) for tested hardware.

## Development

```bash
python3 -m pytest
```

No test requires a receiver, a network, a compositor, or a GPU.

## License

MIT
