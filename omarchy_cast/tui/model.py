"""Presentation logic for the TUI.

Kept separate from the widgets so it can be tested without a terminal.
"""

PROTOCOL_LABELS = {"airplay": "AirPlay", "cast": "Chromecast"}

STATE_STYLE = {
    "idle": "dim",
    "connecting": "yellow",
    "awaiting_pin": "yellow bold",
    "streaming": "green bold",
    "stopping": "yellow",
    "failed": "red bold",
}

STATE_LABELS = {
    "idle": "idle",
    "connecting": "connecting...",
    "awaiting_pin": "PIN required",
    "streaming": "streaming",
    "stopping": "stopping...",
    "failed": "failed",
}


def merge(devices: list[dict], sessions: list[dict]) -> list[dict]:
    """Combine discovered devices with active sessions into display rows.

    Sessions are included even when the device is absent from discovery: a
    device added by raw address may never show up in an mDNS listing.
    """
    by_id = {d["id"]: {**d, "state": "idle"} for d in devices}

    for session in sessions:
        row = by_id.get(session["id"])
        if row is None:
            row = {
                "id": session["id"],
                "name": session.get("name", session["id"]),
                "protocol": session.get("protocol", "airplay"),
                "model": None,
                "address": "",
            }
            by_id[session["id"]] = row
        row["state"] = session["state"]
        row["error"] = session.get("error")

    return sorted(
        by_id.values(),
        key=lambda r: (r["protocol"] != "airplay", r["name"].lower()),
    )


def device_row(row: dict) -> tuple[str, str, str, str]:
    """Render one row as (name, protocol, model, state)."""
    state = row.get("state", "idle")
    return (
        row["name"],
        PROTOCOL_LABELS.get(row["protocol"], row["protocol"]),
        row.get("model") or "-",
        STATE_LABELS.get(state, state),
    )


def session_summary(sessions: list[dict]) -> str:
    if not sessions:
        return "Not casting"

    failed = [s for s in sessions if s.get("state") == "failed"]
    if failed:
        return f"Failed: {failed[0].get('error') or 'unknown error'}"

    if len(sessions) == 1:
        s = sessions[0]
        return f"{STATE_LABELS.get(s['state'], s['state'])} -> {s['name']}"

    names = ", ".join(s["name"] for s in sessions)
    return f"{len(sessions)} sessions: {names}"


def should_prompt_for_pin(sessions: list[dict]) -> str | None:
    """Return the device id awaiting a PIN, if any."""
    for session in sessions:
        if session.get("state") == "awaiting_pin":
            return session["id"]
    return None
