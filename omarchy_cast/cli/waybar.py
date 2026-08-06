ICON_IDLE = "󰄡"
ICON_ACTIVE = "󰄠"

# Right-click works but is invisible: a user reported "waybar doesn't support
# stopping" when it did. The tooltip is where people actually look.
HINT_IDLE = "Click to cast"
HINT_ACTIVE = "Left-click: menu   Right-click: stop"

CONNECTING_STATES = ("connecting", "awaiting_pin")


def render(sessions: list[dict]) -> dict:
    """Build the waybar JSON payload.

    The indicator stays visible in every state and colour-codes the class,
    matching the other toggle indicators in this waybar config. Hiding it when
    idle would make it impossible to find.
    """
    if not sessions:
        return {
            "text": ICON_IDLE,
            "tooltip": f"Not casting\n{HINT_IDLE}",
            "class": "idle",
        }

    failed = [s for s in sessions if s.get("state") == "failed"]
    if failed:
        reason = failed[0].get("error") or "unknown error"
        return {
            "text": ICON_ACTIVE,
            "tooltip": f"Cast failed: {reason}\n{HINT_ACTIVE}",
            "class": "failed",
        }

    connecting = [s for s in sessions if s.get("state") in CONNECTING_STATES]
    if connecting:
        session = connecting[0]
        if session.get("state") == "awaiting_pin":
            tooltip = f"{session['name']}: enter the PIN shown on the receiver"
        else:
            tooltip = f"Connecting to {session['name']}..."
        return {
            "text": ICON_ACTIVE,
            "tooltip": f"{tooltip}\n{HINT_ACTIVE}",
            "class": "connecting",
        }

    names = ", ".join(s["name"] for s in sessions)
    text = ICON_ACTIVE if len(sessions) == 1 else f"{ICON_ACTIVE} {len(sessions)}"
    # Older daemons (or a stray emit) may omit "mode" entirely; skip the label
    # rather than show a blank pair of parens.
    modes = {s.get("mode") for s in sessions if s.get("mode")}
    label = f" ({'/'.join(sorted(modes))})" if modes else ""
    return {
        "text": text,
        "tooltip": f"Casting to {names}{label}\n{HINT_ACTIVE}",
        "class": "streaming",
    }
