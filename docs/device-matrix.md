# Device matrix

Results from real hardware. Protocol correctness cannot be unit tested, so this
table is the record of what has actually been exercised.

If you test a device, please open an issue with the model, protocol, and what
happened — including failures.

## AirPlay

| Device | Model | Status | Notes |
|---|---|---|---|
| Apple TV 4K (2021, 2nd gen) | `AppleTV11,1` | ✅ Works (direct mode) | Full mirror confirmed: `/info`, `pair-verify`, FairPlay SAP, SETUP phases 1+2, `RECORD` 200, event + data channels, NTP sync, clean TEARDOWN. Captured 1920×1080@30 via NVENC at 4147 kbps for ~56 s. Requires a direct `doubletake -target` run — **daemon mode fails**, see below. Device has an onscreen code and no AirPlay password. |
| Apple TV 4K (2022, 3rd gen) | `AppleTV14,1` | ❓ Untested | Discovered and reachable during testing; not connected to. Listed as working upstream. |
| LG webOS TV | `KWS85U02` | ❓ Untested | Advertises `_airplay._tcp`. Third-party AirPlay receivers are the flakier path. |

## Chromecast

| Device | Model | Status | Notes |
|---|---|---|---|
| — | — | ❓ Untested | No Chromecast was present on any network tested. The Cast backend is covered by unit tests against fakes but has never talked to real hardware. |

## Capture and encode

Verified on the development machine (ASUS ROG Zephyrus G16 GU605CR, Intel iGPU,
2560×1600 @ 30 fps):

| Component | Status | Notes |
|---|---|---|
| xdg-desktop-portal-hyprland ScreenCast | ✅ | `CreateSession` → `SelectSources` → `Start` → `OpenPipeWireRemote` |
| Portal restore token | ✅ | Replaying a stored token suppresses the prompt entirely |
| `pipewiresrc` | ✅ | |
| `vah264enc` (VA-API) | ✅ | 6.8–7.4 Mbps at `bitrate=8000` |
| `appsink` → Python chunks | ✅ | 255 chunks in 4 s |
| Output decodes | ✅ | 197 H.264 frames, 2560×1600, 30 fps, confirmed with ffprobe |
| Portal handshake → first chunk | ✅ | 0.75 s, no user interaction |

Note: `vah264enc` with `rate-control=cbr` and no explicit `bitrate`
auto-calculated **21.4 Mbps**, enough to saturate a weak link. An explicit
bitrate is always set now.

## Confirmed upstream bug: `-port-range` ignored in daemon mode

doubletake 0.4.0's `daemon.Config` (internal/daemon/daemon.go) has no
`PortMin`/`PortMax` fields. `-port-range` is parsed in `cmd/doubletake/main.go`
and passed into `StreamConfig` on the direct path only, so in `-daemonize` mode
it is silently dropped.

Measured on the same device, same flags, minutes apart:

| Mode | Ports actually bound | Result |
|---|---|---|
| `-daemonize` + `doubletake-ctl connect` | UDP 36760-36762, TCP 45771 | SETUP stalls, then `HTTP 401` / i/o timeout |
| `doubletake -target` | UDP 60000-60002, TCP 60003 | Mirrors successfully |

With a default-DROP firewall only the second works. This is the real cause of
every AirPlay failure recorded in this project. Two earlier explanations —
*Require Password*, then *Require Device Verification* — were both wrong.

## Networks

| Condition | Result |
|---|---|
| mDNS on a flat corporate LAN | ✅ Discovery found 18 service records across `_airplay`, `_raop` |
| mDNS on an AP that does not forward multicast | ❌ Zero records for a receiver with ports 7000/7100/5000 open — use `--address` |
| ufw with default-DROP INPUT | ❌ Blocks the AirPlay return connection; see the firewall section in the README |
