#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${HOME}/.local/share/omarchy-cast"
CFG_DIR="${HOME}/.config/omarchy-cast"
STATE_DIR="${HOME}/.local/state/omarchy-cast"

msg()  { printf '\e[1;34m[omarchy-cast]\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[omarchy-cast]\e[0m %s\n' "$*" >&2; }

msg "stopping any running session"
if command -v omarchy-cast >/dev/null 2>&1; then
  omarchy-cast stop >/dev/null 2>&1 || true
fi

msg "removing launchers from $BIN_DIR"
for name in omarchy-cast omarchy-castd omarchy-cast-tui; do
  rm -f "$BIN_DIR/$name"
done

msg "removing $SHARE_DIR"
rm -rf "$SHARE_DIR"

msg "removing $STATE_DIR (portal restore token)"
rm -rf "$STATE_DIR"

# Config is left in place deliberately: it is the user's, not ours.
if [[ -d "$CFG_DIR" ]]; then
  msg "leaving your config at $CFG_DIR — remove it yourself if you want it gone"
fi

cat <<EOF

$(msg "uninstalled")

Not removed automatically, since they are edits to your own files:
  - the "custom/cast-indicator" block in ~/.config/waybar/config.jsonc
  - the cast-indicator rules in ~/.config/waybar/style.css
  - the keybinding in ~/.config/hypr/bindings.conf
  - the ufw rules for ports 60000-60010 and 8010
  - doubletake and its credentials in ~/.config/doubletake
EOF
