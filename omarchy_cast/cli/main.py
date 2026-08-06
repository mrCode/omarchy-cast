import argparse
import asyncio
import json
import subprocess
import sys

from omarchy_cast.core.notify import notify
from omarchy_cast.core.session import EXTEND, MIRROR, MODES
from omarchy_cast.cli.client import DaemonUnavailable, request
from omarchy_cast.cli.menu import (
    MANUAL_ENTRY,
    MODE_ENTRIES,
    STOP_ENTRY,
    format_entries,
    parse_mode,
    parse_selection,
)
from omarchy_cast.cli.waybar import render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omarchy-cast")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list discovered receivers")
    sub.add_parser("status", help="show active sessions")

    start = sub.add_parser("start", help="start mirroring to a receiver")
    start.add_argument("device_id", nargs="?", default=None)
    # Required because some access points do not forward multicast, leaving a
    # reachable receiver invisible to mDNS.
    start.add_argument("--address", help="connect by raw IP instead of discovery")
    start.add_argument(
        "--protocol",
        default="airplay",
        choices=("airplay", "cast"),
        help="protocol to use with --address (default: airplay)",
    )
    start.add_argument("--name", help="display name for a device added by address")
    start.add_argument(
        "--mode",
        default=MIRROR,
        choices=MODES,
        help="mirror the screen (default) or extend onto a virtual display",
    )

    stop = sub.add_parser("stop", help="stop mirroring (all sessions if no device given)")
    stop.add_argument("device_id", nargs="?", default=None)

    pin = sub.add_parser("pin", help="submit a pairing PIN")
    pin.add_argument("device_id")
    pin.add_argument("pin")

    sub.add_parser("waybar", help="print waybar JSON for the cast indicator")
    sub.add_parser("menu", help="pick a receiver via walker and start casting")

    return parser


def _notify(message: str, urgent: bool = False) -> None:
    """Errors here are answers to a command the user just ran, so they are not
    urgent -- the user is already looking. See core.notify for why that matters."""
    notify(message, urgent=urgent)


def _run_waybar() -> int:
    """Never spawns the daemon: waybar polls this every couple of seconds."""
    try:
        response = asyncio.run(request("status", autospawn=False))
    except DaemonUnavailable:
        print(json.dumps(render([])))
        return 0
    sessions = (response.get("data") or {}).get("sessions", [])
    print(json.dumps(render(sessions)))
    return 0


def _walker(entries: list[str], prompt: str) -> str:
    result = subprocess.run(
        ["walker", "--dmenu", "-p", prompt],
        input="\n".join(entries),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _prompt_for_address() -> str:
    return _walker([], "Receiver IP address").strip()


def _prompt_mode() -> str | None:
    return parse_mode(_walker(list(MODE_ENTRIES), "Mirror or extend?"))


def _run_menu() -> int:
    response = asyncio.run(request("list"))
    if not response.get("ok"):
        return _fail(response.get("error", "unknown error"))

    devices = (response.get("data") or {}).get("devices", [])

    status = asyncio.run(request("status"))
    sessions = (status.get("data") or {}).get("sessions", [])

    prompt = "Casting — pick to stop, or cast elsewhere" if sessions else "Cast to"
    selection = _walker(format_entries(devices, sessions), prompt).strip()
    if not selection:
        return 0

    if selection.startswith(STOP_ENTRY):
        result = asyncio.run(request("stop"))
        if not result.get("ok"):
            message = result.get("error", "unknown error")
            _notify(message)
            return _fail(message)
        _notify("Stopped casting")
        return 0

    if selection == MANUAL_ENTRY:
        address = _prompt_for_address()
        if not address:
            return 0
        # Mode is asked before "add" registers anything, so cancelling here
        # leaves no orphaned device behind (see the other branch, which has
        # nothing to register in the first place).
        mode = _prompt_mode()
        if mode is None:
            return 0
        added = asyncio.run(request("add", address=address, protocol="airplay"))
        if not added.get("ok"):
            message = added.get("error", "unknown error")
            _notify(message)
            return _fail(message)
        device_id = (added.get("data") or {})["device"]["id"]
    else:
        device_id = parse_selection(selection)
        if device_id is None:
            return 0
        mode = _prompt_mode()
        if mode is None:
            return 0

    result = asyncio.run(request("start", device_id=device_id, mode=mode))
    if not result.get("ok"):
        message = result.get("error", "unknown error")
        _notify(message)
        return _fail(message)

    warning = (result.get("data") or {}).get("warning")
    if mode == EXTEND:
        # The warning (e.g. Chromecast is untested) must not swallow this:
        # picking the wrong output at the portal prompt silently mirrors
        # instead of extending, and that choice then repeats on every cast.
        # Both messages have to reach the user, so compose one notification
        # rather than let one replace the other.
        hint = (
            "Extending — if the portal asks, share the 'omarchy-cast' output. "
            "Right-click the waybar icon to stop."
        )
        _notify(f"{warning}\n{hint}" if warning else hint)
    elif warning:
        _notify(warning)
    else:
        # The tooltip carries the same hint, but only on hover.
        _notify("Casting started — right-click the waybar icon to stop")
    return 0


def _print_devices(devices: list[dict]) -> None:
    if not devices:
        print("no receivers found")
        return
    for d in devices:
        model = f" ({d['model']})" if d.get("model") else ""
        print(f"{d['id']:<28} {d['name']}{model}")


def _print_sessions(sessions: list[dict]) -> None:
    if not sessions:
        print("not casting")
        return
    for s in sessions:
        suffix = f" - {s['error']}" if s.get("error") else ""
        print(f"{s['name']} [{s['protocol']}] {s['state']}{suffix}")


def _fail(message: str, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def _resolve_by_address(args) -> tuple[str | None, int]:
    """Register a raw address with the daemon, returning its device id."""
    response = asyncio.run(
        request(
            "add",
            address=args.address,
            protocol=args.protocol,
            name=args.name,
        )
    )
    if not response.get("ok"):
        return None, _fail(response.get("error", "unknown error"))
    return (response.get("data") or {})["device"]["id"], 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "waybar":
        return _run_waybar()
    if args.command == "menu":
        return _run_menu()

    try:
        if args.command == "start":
            if not args.device_id and not args.address:
                parser.error("start requires a device id or --address")
            device_id = args.device_id
            if not device_id:
                device_id, code = _resolve_by_address(args)
                if device_id is None:
                    return code
            response = asyncio.run(request("start", device_id=device_id, mode=args.mode))
        elif args.command == "pin":
            response = asyncio.run(
                request("pin", device_id=args.device_id, pin=args.pin)
            )
        elif args.command == "stop":
            response = asyncio.run(request("stop", device_id=args.device_id))
        else:
            response = asyncio.run(request(args.command))
    except DaemonUnavailable as exc:
        return _fail(str(exc), code=2)

    if not response.get("ok"):
        return _fail(response.get("error", "unknown error"))

    data = response.get("data") or {}
    if data.get("warning"):
        print(data["warning"], file=sys.stderr)
    if args.command == "list":
        _print_devices(data.get("devices", []))
    elif args.command == "status":
        _print_sessions(data.get("sessions", []))
    elif args.command == "stop":
        print(f"stopped {data.get('stopped', 0)} session(s)")
    else:
        print(data.get("state", "ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
