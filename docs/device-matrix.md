# Device matrix

Results from real hardware. Protocol correctness cannot be unit tested, so this
table is the record of what has actually been exercised.

If you test a device, please open an issue with the model, protocol, and what
happened — including failures.

## AirPlay

| Device | Model | Status | Notes |
|---|---|---|---|
| Apple TV 4K (2021, 2nd gen) | `AppleTV11,1` | ⚠️ Blocked | Discovery, direct-IP connect, onscreen-code (SRP-6a) pairing, credential persistence and `pair-verify` all succeed. The mirroring SETUP is then separately rejected with `HTTP 401`. **The device has no AirPlay password — it requires an onscreen code**, so this is not the fixed-password case, and the earlier note claiming otherwise was wrong. Cause not yet confirmed; `WWW-Authenticate` on the 401 has not been captured. Related: [doubletake#26](https://github.com/omarroth/doubletake/issues/26). |
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

## Open question

The `AppleTV11,1` 401 above is unexplained. Pairing succeeds, so it is not a
missing-pairing problem. Capturing the `WWW-Authenticate` header from
`doubletake -target <ip> -debug` would say whether the receiver is asking for
RFC 2069 Digest (the case [doubletake#26](https://github.com/omarroth/doubletake/issues/26)
addresses) or something else. Until that is captured, do not assume #26 fixes it.

## Networks

| Condition | Result |
|---|---|
| mDNS on a flat corporate LAN | ✅ Discovery found 18 service records across `_airplay`, `_raop` |
| mDNS on an AP that does not forward multicast | ❌ Zero records for a receiver with ports 7000/7100/5000 open — use `--address` |
| ufw with default-DROP INPUT | ❌ Blocks the AirPlay return connection; see the firewall section in the README |
