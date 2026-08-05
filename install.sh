#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE_DIR="${HOME}/.local/share/omarchy-cast"
CFG_DIR="${HOME}/.config/omarchy-cast"

msg()  { printf '\e[1;34m[omarchy-cast]\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[omarchy-cast]\e[0m %s\n' "$*" >&2; }
err()  { printf '\e[1;31m[omarchy-cast]\e[0m %s\n' "$*" >&2; exit 1; }

check_dep() {
  command -v "$1" >/dev/null 2>&1 || warn "missing: $1 — install with: $2"
}

check_python_mod() {
  python3 -c "import $1" 2>/dev/null || warn "missing python module: $1 — install with: $2"
}

check_gst_element() {
  gst-inspect-1.0 "$1" >/dev/null 2>&1 || warn "missing GStreamer element: $1 — install with: $2"
}

msg "verifying runtime deps"
check_dep python3      "pacman -S python"
check_dep walker       "pacman -S walker"
check_dep waybar       "pacman -S waybar"
check_dep notify-send  "pacman -S libnotify"
check_dep gst-inspect-1.0 "pacman -S gstreamer"

check_python_mod pychromecast "pacman -S python-pychromecast"
check_python_mod zeroconf     "pacman -S python-zeroconf"
check_python_mod gi           "pacman -S python-gobject"
check_python_mod textual      "pacman -S python-textual"

# gst-plugin-va is NOT pulled in by doubletake, and vah264enc is the default
# encoder. Without it we silently fall back to software x264.
check_gst_element pipewiresrc "pacman -S gst-plugin-pipewire"
check_gst_element vah264enc   "pacman -S gst-plugin-va"
check_gst_element x264enc     "pacman -S gst-plugins-ugly"

if ! command -v doubletake >/dev/null 2>&1; then
  warn "doubletake not found — AirPlay will be unavailable"
  warn "  install it with: yay -S doubletake"
fi

command -v python3 >/dev/null 2>&1 || err "python3 is required"

msg "installing package"
python3 -m pip install --user --upgrade "$PROJECT_DIR" >/dev/null

msg "installing waybar snippets → $SHARE_DIR"
mkdir -p "$SHARE_DIR/waybar" "$CFG_DIR"
cp "$PROJECT_DIR"/share/waybar/* "$SHARE_DIR/waybar/"

if [[ ! -f "$CFG_DIR/config.toml" ]]; then
  msg "writing default config → $CFG_DIR/config.toml"
  cat > "$CFG_DIR/config.toml" <<'EOF'
[capture]
fps = 30
encoder = "auto"

[airplay]
port_range = "60000-60010"

[cast]
bitrate = 8000
EOF
fi

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "$HOME/.local/bin is not on your PATH" ;;
esac

cat <<EOF

$(msg "installed")

Next steps — these are NOT applied automatically:

1. Waybar. Add "custom/cast-indicator" to modules-right in
   ~/.config/waybar/config.jsonc, merge in:
     $SHARE_DIR/waybar/cast-indicator.jsonc
   and append to ~/.config/waybar/style.css:
     $SHARE_DIR/waybar/cast-indicator.css

2. Keybinding. In ~/.config/hypr/bindings.conf:
     bindd = SUPER ALT, C, Cast screen, exec, omarchy-cast menu

3. Firewall. AirPlay stalls silently without this — the receiver connects
   back to this machine:
     sudo ufw allow proto tcp from <your-subnet> to any port 60000:60010
     sudo ufw allow proto udp from <your-subnet> to any port 60000:60010

Then try:  omarchy-cast list
EOF
