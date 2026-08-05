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
yay -S omarchy-cast
```

AirPlay support additionally needs [doubletake](https://github.com/omarroth/doubletake):

```bash
yay -S doubletake
```

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

Replace `192.168.1.0/24` with your own subnet. Scope it to the receiver's IP
instead if you prefer a tighter rule — but remember DHCP can move it.

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
code = ""                                 # see "Password-protected receivers"

[cast]
bitrate = 8000                            # kbps
http_port = 0                             # 0 = ephemeral
```

NVENC is ranked **last** on purpose. On laptops whose display runs off the
integrated GPU, encoding on the discrete GPU forces a cross-GPU copy of every
frame, which costs more latency than the faster encoder saves. Override
`encoder_ranking` if your display is driven by the dGPU.

## Known limitations

**Password-protected AirPlay receivers do not work.** A receiver with *Require
Password* enabled (Settings → AirPlay and HomeKit on an Apple TV) challenges the
mirroring setup with HTTP Digest auth, which doubletake 0.4.0 cannot answer. It
fails with `HTTP 401`. This affects most shared and corporate Apple TVs. Support
is an open upstream PR, [doubletake#26](https://github.com/omarroth/doubletake/issues/26);
the `code` config key is already plumbed for when it lands.

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
