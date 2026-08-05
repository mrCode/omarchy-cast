# omarchy-cast

Mirror your Omarchy desktop to an Apple TV or a Chromecast, from a walker menu
with a waybar indicator.

Built for Hyprland/Wayland. Screen capture goes through xdg-desktop-portal and
PipeWire, encoded on the GPU.

## Latency — read this before installing

The two protocols are not equivalent, and the difference is large:

| Target | Latency | Good for |
|---|---|---|
| **AirPlay** (Apple TV) | ~100–300 ms | Anything, including a usable second screen |
| **Chromecast** | **1–3 seconds** | Video, slides, presentations |

Chromecast casting goes through the Default Media Receiver, which buffers a
media stream. **It is not usable as a second display** and it is not suitable
for anything interactive — you will see your cursor move a second or more after
you move it. This is a property of the protocol path, not a bug to be fixed.

Real low-latency Chromecast mirroring needs the Chrome Mirroring receiver app,
which requires AES-CTR-128 that GStreamer's SRTP elements do not implement. See
`docs/superpowers/specs/` for the full reasoning.

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

**AirPlay requires the display resolution to match what doubletake negotiates.**
doubletake negotiates the AirPlay stream at 1920x1080, and its software capture
path has no scaler — so on a higher-resolution display it sends full-resolution
video into a 1080p stream and the receiver drops the connection immediately
(`broken pipe` right after the codec frame).

Until that is fixed upstream, set the output to 1920x1080 while casting:

```bash
hyprctl keyword monitor eDP-2,1920x1080@60,0x0,1
```

and restore it afterwards, e.g.:

```bash
hyprctl keyword monitor eDP-2,2560x1600@240,0x0,1.6
```

Confirmed on an `AppleTV11,1`: at 2560x1600 the receiver hung up on the codec
frame; at 1920x1080 the same setup mirrored at a sustained 31 fps.

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
