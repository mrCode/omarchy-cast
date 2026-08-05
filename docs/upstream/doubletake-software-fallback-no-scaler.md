# Draft issue for omarroth/doubletake

Not filed. Review, edit, and submit if you want it.

---

**Title:** Software capture fallback has no scaler, so the receiver drops the connection on high-DPI displays

---

When `vapostproc` is unavailable and capture falls back to `videoconvert`, the
pipeline emits frames at the display's native resolution while the AirPlay
stream has been negotiated at 1920x1080. The receiver closes the data channel
immediately on the codec frame.

```
[CAPTURE] vapostproc unavailable, using software conversion
[CAPTURE] auto bitrate selected: 4147 kbps for 1920x1080@30fps
[STREAM] encoded content size from SPS: 2560x1600
[SEND] codec frame: seq=1 payLen=51
streaming error: send codec: writev tcp ...: broken pipe
```

Note the mismatch: the bitrate and stream are sized for 1920x1080, but the SPS
says 2560x1600 — the panel's native mode.

## Environment

- doubletake `0.4.0.r11.g568a06e` (AUR `doubletake-git`)
- Arch Linux, Hyprland/Wayland, PipeWire, `eDP-2` at 2560x1600
- Receiver: Apple TV 4K 2nd gen (`AppleTV11,1`)

## Reproduce

On a display whose native resolution is not 1920x1080, with `vapostproc`
unavailable (or made to appear so):

```sh
doubletake -target <ip> -hwaccel vaapi -debug
```

## Cause

`vapostproc` was doing the downscale as well as the pixel-format conversion. The
fallback branch in `internal/airplay/capture.go` substitutes `videoconvert`,
which converts format only:

```go
if hasGstElement("vapostproc") {
    ... "!", "vapostproc",
} else {
    log.Printf("[CAPTURE] vapostproc unavailable, using software conversion")
}
... "!", "videoconvert",
```

so nothing constrains the output resolution on that path.

## Workaround

Setting the output to 1920x1080 makes the SPS match the negotiated stream and it
mirrors correctly — sustained 30 fps, verified over several minutes:

```sh
hyprctl keyword monitor eDP-2,1920x1080@60,0x0,1
```

## Suggested fix

Add `videoscale` plus a caps filter pinning the negotiated resolution to the
fallback branch, so both paths produce the same output geometry. Something like:

```
... ! videoconvert ! videoscale ! video/x-raw,width=1920,height=1080,format=NV12 ! ...
```

using whatever resolution the stream was negotiated at rather than a constant.

It would also help to fail loudly rather than send a mismatched SPS — the
current failure is a `broken pipe` several steps removed from the cause, and on
the receiver it looks like a black screen.

## Related

Found while chasing a separate problem: `vapostproc` cannot import Hyprland's
padded DMA-BUF (`fd size (16777216) is bigger than object descriptor size
(16384000)`, where 16384000 = 2560x1600x4), which is what pushed this setup onto
the fallback path in the first place. Happy to open that separately if useful —
it may warrant detecting the failure and falling back automatically, since
`gst-launch --quiet` currently hides it and the symptom is a silent black
screen.
