# Extend mode — Design

**Date:** 2026-08-06
**Status:** Approved, pending implementation plan
**Supersedes:** the "virtual-display casting" non-goal in
`2026-08-05-omarchy-cast-design.md`

## Summary

A cast is either a **mirror** (the receiver shows the laptop screen, today's
behaviour) or an **extend** (the receiver becomes a second monitor with its own
workspaces). Mode is chosen in the menu each time a cast starts.

Extend works by creating a Hyprland virtual output named `omarchy-cast` at
1920x1080, casting that instead of the laptop panel, and removing it when the
cast ends.

## Why this is worth doing

Beyond being the feature asked for, extend avoids the ugliest part of mirror
mode. doubletake negotiates AirPlay at 1920x1080 and its capture path has no
scaler, so mirroring a 2560x1600 panel requires switching the whole display to
1080p for the duration (see `docs/upstream/doubletake-software-fallback-no-scaler.md`).
A Hyprland virtual output is created at 1920x1080 natively, so extend needs none
of that: the laptop display is never touched.

## Verified before designing

Run on the target machine 2026-08-06:

- `hyprctl output create headless` works and the output can be removed cleanly.
- **`hyprctl output create headless <name>` accepts a name.** Undocumented in
  `hyprctl output --help`, which lists only the backend argument. This matters:
  without it outputs are named `HEADLESS-1`, `HEADLESS-2`, … with an
  incrementing counter, so the portal restore token would reference a
  non-existent output on every subsequent run and re-prompt forever.
- The output defaults to scale 2.0 (logical 960x540) and must be set to scale 1
  to be a usable desktop.
- Removing an output returns its workspace to the remaining monitor.
- **doubletake accepts `-creds <path>`**, which is what makes per-mode output
  selection possible without editing its credential store.

A stray `HEADLESS-2` was left behind during testing by assuming the created name
rather than discovering it. The implementation must diff the monitor list.

## Goals

- Choose mirror or extend when starting a cast.
- Extend gives a real second monitor: own workspaces, windows draggable onto it.
- The laptop display is never modified in extend mode.
- Nothing is left behind when a cast ends, fails, or the daemon dies.

## Non-goals

- Moving windows onto the virtual output automatically.
- Remembering a per-device mode preference.
- Position or layout control beyond "auto, to the right of existing outputs".
- Extend for Chromecast is implemented but **not claimed to work**: the Cast
  backend has never run against real hardware.

## Architecture

### Mode

`CastMode` is `"mirror"` or `"extend"`, defaulting to `"mirror"`. It travels
with the start request, is stored on the `Session`, and is reported by `status`
so every client can display it.

### `core/virtual_display.py`

The only module that knows about `hyprctl output`. Backends never call it
directly, mirroring how `core/display.py` isolates mode switching.

```python
VIRTUAL_NAME = "omarchy-cast"

available() -> bool                  # hyprctl present
create(runner=...) -> str            # create, configure, return the ACTUAL name
remove(name, runner=...) -> bool
cleanup_strays(runner=...) -> int    # remove leftovers at daemon start
```

`create()` diffs the monitor list before and after, and returns the name it
actually observes rather than the name it requested. Configuration is
`<name>,1920x1080@60,auto,1` — `auto` places it right of existing outputs, and
scale 1 yields a logical 1920x1080.

### `backends/creds.py`

Decides which credentials file doubletake is given.

```python
creds_path(mode) -> Path | None      # None => doubletake's own default (mirror)
ensure_extend_creds() -> Path        # copy the mirror file, strip restore_token
```

doubletake stores one `restore_token` per device, so mirror and extend would
otherwise overwrite each other's output selection. Giving extend its own file
via `-creds` keeps them independent without editing doubletake's store or
depending on its JSON layout beyond removing one key from **our copy**.

Copying preserves the pairing, so extend does not require a second PIN.
Stripping `restore_token` forces one portal prompt on first extend use, after
which the token in that file is stable because the output name is stable.

### `AirPlayBackend`

`start(device, mode)`:

- **mirror** — unchanged, including the display switch.
- **extend** — create the virtual output, pass `-creds <extend path>`, and skip
  display switching entirely.

Teardown removes the virtual output on all five existing paths: stop, start
failure, ready timeout, crash detection, daemon shutdown.

### Cast

`CastBackend` gets the same mode parameter. Our own portal token is stored per
mode, which is simpler than doubletake's case because we own the file. Shipped
untested, consistent with how the Cast backend is already described.

## Data flow

### Extend start

1. Client sends `start(device_id, mode="extend")`.
2. Daemon resolves the device and calls `backend.start(device, mode)`.
3. Backend removes any stray virtual outputs, then creates `omarchy-cast`.
4. Backend ensures the extend credentials file exists.
5. doubletake is spawned with `-creds <extend path>`; the display is untouched.
6. First run only: the portal asks which output to share. The user picks
   `omarchy-cast`; the token is saved into the extend credentials file.
7. `screen capture started` → session becomes STREAMING.

### Extend stop

Terminate the child process group, then remove the virtual output. Hyprland
returns its workspace to the laptop.

## Error handling

| Failure | Handling |
|---|---|
| `hyprctl` unavailable / not Hyprland | Extend refused with a clear reason; mirror still works |
| Virtual output creation fails | Session fails; no output left behind |
| Stray virtual output after a crash | `cleanup_strays()` at daemon start |
| Windows present when the output is removed | Hyprland reparents them; verified |
| Extend to two devices at once | Rejected with a clear "already extending to <device>" error. One extend at a time; mirroring to several receivers at once is unaffected. Refcounted sharing was considered and dropped as unnecessary complexity for a single-laptop use case. |
| Restore token references a deleted output | Portal re-prompts, which is correct |

### The most likely real failure

On the first portal prompt, choosing `eDP-2` instead of `omarchy-cast` silently
produces a mirror, and the stored token then repeats that mistake on every run.
It presents as "extend is broken".

Three mitigations, none of which can prevent it outright:

- The output is named `omarchy-cast`, so it is identifiable in the picker.
- The notification shown when extend starts names the output to choose.
- `status` reports the mode, so the wrong pick is visible rather than mysterious.

Recovery is deleting the extend credentials file, which forces a fresh prompt.
This is worth documenting in the README.

## Testing

`virtual_display.py` and `creds.py` take an injected command runner and temp
paths. No test creates a real output or invokes `hyprctl`, following the
precedent set after backend tests were found switching the real monitor.

Backend tests cover mode plumbing against fakes: that extend creates and removes
the output, that mirror does not, that extend does not switch the display, and
that every teardown path removes the output.

What tests cannot cover: whether the receiver actually shows a *separate*
desktop rather than a copy of the laptop. That is a manual check recorded in
`docs/device-matrix.md`.

## UI

- **Menu** — device, then a second prompt: Mirror or Extend.
- **CLI** — `omarchy-cast start <id> --mode extend`, default `mirror`.
- **TUI** — `e` starts the highlighted device in extend mode; `Enter` mirrors.
- **waybar** — tooltip reports the mode alongside the device name.

## Risks

- **Undocumented Hyprland behaviour.** Naming a virtual output is not in
  `hyprctl output --help`. If a future Hyprland drops it, output names revert to
  `HEADLESS-N` and the restore token breaks on every run. The implementation
  should detect that the created name differs from the requested one and warn.
- **doubletake's `-creds` is a documented flag**, so this is lower risk than the
  `gst-inspect` shim, but it is still a second behavioural dependency on an
  actively changing project.
- **Extend cannot be verified by tests.** Both modes look identical from the
  process table; only the receiver shows the difference.
