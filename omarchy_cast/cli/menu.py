import re

LABELS = {"airplay": "AirPlay", "cast": "Chromecast"}

# Offered because mDNS is unusable on networks that do not forward multicast.
MANUAL_ENTRY = "Enter an address manually..."

# Right-click on the waybar module is undiscoverable and did not work for at
# least one user, so stopping must be reachable from the menu itself.
STOP_ENTRY = "Stop casting"

# Ids embed colons (AirPlay uses a MAC), so anchor on the trailing brackets.
ID_PATTERN = re.compile(r"\[((?:airplay|cast):.+)\]$")


def format_entries(devices: list[dict], sessions: list[dict] | None = None) -> list[str]:
    ordered = sorted(devices, key=lambda d: (d["protocol"] != "airplay", d["name"].lower()))
    entries = []
    for session in sessions or []:
        # First, so stopping is always one click away while casting.
        entries.append(f"{STOP_ENTRY} ({session['name']})")
    for d in ordered:
        label = LABELS.get(d["protocol"], d["protocol"])
        model = f" · {d['model']}" if d.get("model") else ""
        entries.append(f"{d['name']} ({label}{model}) [{d['id']}]")
    entries.append(MANUAL_ENTRY)
    return entries


def parse_selection(line: str) -> str | None:
    match = ID_PATTERN.search(line.strip())
    return match.group(1) if match else None
