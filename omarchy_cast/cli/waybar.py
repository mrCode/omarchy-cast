ICON_IDLE = "󰄡"
ICON_ACTIVE = "󰄠"

CONNECTING_STATES = ("connecting", "awaiting_pin")


def render(sessions: list[dict]) -> dict:
    """Build the waybar JSON payload.

    The indicator stays visible in every state and colour-codes the class,
    matching the other toggle indicators in this waybar config. Hiding it when
    idle would make it impossible to find.
    """
    if not sessions:
        return {"text": ICON_IDLE, "tooltip": "Not casting", "class": "idle"}

    failed = [s for s in sessions if s.get("state") == "failed"]
    if failed:
        reason = failed[0].get("error") or "unknown error"
        return {
            "text": ICON_ACTIVE,
            "tooltip": f"Cast failed: {reason}",
            "class": "failed",
        }

    connecting = [s for s in sessions if s.get("state") in CONNECTING_STATES]
    if connecting:
        session = connecting[0]
        if session.get("state") == "awaiting_pin":
            tooltip = f"{session['name']}: enter the PIN shown on the receiver"
        else:
            tooltip = f"Connecting to {session['name']}..."
        return {"text": ICON_ACTIVE, "tooltip": tooltip, "class": "connecting"}

    names = ", ".join(s["name"] for s in sessions)
    text = ICON_ACTIVE if len(sessions) == 1 else f"{ICON_ACTIVE} {len(sessions)}"
    return {"text": text, "tooltip": f"Casting to {names}", "class": "streaming"}
