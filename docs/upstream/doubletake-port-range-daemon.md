# Draft issue for omarroth/doubletake

Not filed. Review, edit, and submit if you want it.

---

**Title:** `-port-range` is ignored in `-daemonize` mode, breaking mirroring behind a firewall

---

`-port-range` has no effect when running with `-daemonize`. The receiver's
reverse handshake ends up on OS-ephemeral ports instead of the requested range,
so on any host with a default-DROP firewall the SETUP stalls and mirroring never
starts — while the same device mirrors fine via `doubletake -target`.

This is easy to mistake for an authentication or pairing problem: pairing
succeeds, credentials persist, `pair-verify` succeeds, and only then does SETUP
fail. In my case it surfaced as `HTTP 401`, which sent me looking at the Apple
TV's AirPlay password settings for a while before I found the real cause.

## Environment

- doubletake 0.4.0 (Arch, AUR `doubletake`), also present on `master`
- Arch Linux, Hyprland/Wayland, PipeWire, ufw active with default-DROP INPUT
- Receiver: Apple TV 4K 2nd gen (`AppleTV11,1`), onscreen code enabled, no
  AirPlay password

## Reproduce

With a firewall allowing inbound TCP+UDP on 60000-60010 from the receiver and
dropping everything else:

```sh
# fails
doubletake -daemonize -no-audio -debug -port-range 60000-60010
doubletake-ctl connect 192.168.1.231

# works, same flags, same device
doubletake -target 192.168.1.231 -no-audio -debug -port-range 60000-60010
```

## Observed

Daemon mode ignores the range and binds ephemeral ports:

```
[SETUP] consecutive UDP ports: timing=36760 ctrl=36761 data=36762
[SETUP] event listener on TCP port 45771
warning: Apple TV at 192.168.1.231 has not responded to SETUP after 3s.
  this usually means a host firewall is blocking the receiver's reverse handshake.
  re-run with -port-range MIN-MAX (e.g. -port-range 60000-60010) and allow that
  range inbound (UDP+TCP) from 192.168.1.231.
[daemon] mirror setup failed: SETUP phase 1 (audio): read encrypted response
  frame 0: ... i/o timeout
```

Note the warning advises passing `-port-range`, which was already passed.

Direct mode honours it and mirrors successfully:

```
[SETUP] consecutive UDP ports: timing=60000 ctrl=60001 data=60002
[SETUP] event listener on TCP port 60003
...
mirror session ready (data port: 49277)
screen capture started
```

| Mode | Ports bound | Result |
|---|---|---|
| `-daemonize` | UDP 36760-36762, TCP 45771 | stalls, then `HTTP 401` / i/o timeout |
| `-target` | UDP 60000-60002, TCP 60003 | mirrors successfully |

## Cause

`portRange` is parsed in `cmd/doubletake/main.go` but is not among the arguments
passed to `runDaemon`:

```go
if *daemonize {
    runDaemon(*socketPath, *credFile, *credBackend, *fps, *bitrate, *hwaccel,
              *debug, *testMode, *noEncrypt, *directKey, *noAudio, *noCursor)
    return
}
```

so it never reaches `daemon.Config`, which has no port fields
(`internal/daemon/daemon.go`):

```go
type Config struct {
    SocketPath  string
    CredFile    string
    CredBackend string
    FPS         int
    Bitrate     int
    HWAccel     string
    Debug       bool
    TestMode    bool
    NoEncrypt   bool
    DirectKey   bool
    NoAudio     bool
    ShowCursor  bool
}
```

On the direct path the same value is threaded into `StreamConfig` and works:

```go
portMin, portMax, err := parsePortRange(*portRange)
streamCfg := airplay.StreamConfig{
    ...
    PortMin: portMin,
    PortMax: portMax,
}
```

## Suggested fix

Add `PortMin`/`PortMax` to `daemon.Config`, pass `*portRange` (or the parsed
bounds) into `runDaemon`, and set them on the `StreamConfig` the daemon builds
for each connection, matching the direct path.

Parsing in `main()` before branching would also surface an invalid
`-port-range` in daemon mode, which currently is not validated at all.

## Why it matters

`-port-range` exists specifically so the receiver's reverse connection can pass
a firewall. In daemon mode it silently does nothing, so the documented remedy
for the most common AirPlay failure is unavailable in the mode the README
recommends for multi-target use — and the failure it produces looks like an
authentication problem rather than a networking one.

Happy to send a PR if you'd like.
