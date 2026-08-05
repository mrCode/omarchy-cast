import re

LABELS = {"airplay": "AirPlay", "cast": "Chromecast"}

# Offered because mDNS is unusable on networks that do not forward multicast.
MANUAL_ENTRY = "Enter an address manually..."

# Ids embed colons (AirPlay uses a MAC), so anchor on the trailing brackets.
ID_PATTERN = re.compile(r"\[((?:airplay|cast):.+)\]$")


def format_entries(devices: list[dict]) -> list[str]:
    ordered = sorted(devices, key=lambda d: (d["protocol"] != "airplay", d["name"].lower()))
    entries = []
    for d in ordered:
        label = LABELS.get(d["protocol"], d["protocol"])
        model = f" · {d['model']}" if d.get("model") else ""
        entries.append(f"{d['name']} ({label}{model}) [{d['id']}]")
    entries.append(MANUAL_ENTRY)
    return entries


def parse_selection(line: str) -> str | None:
    match = ID_PATTERN.search(line.strip())
    return match.group(1) if match else None
