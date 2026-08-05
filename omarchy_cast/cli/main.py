import argparse
import asyncio
import sys

from omarchy_cast.cli.client import DaemonUnavailable, request


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

    stop = sub.add_parser("stop", help="stop mirroring (all sessions if no device given)")
    stop.add_argument("device_id", nargs="?", default=None)

    pin = sub.add_parser("pin", help="submit a pairing PIN")
    pin.add_argument("device_id")
    pin.add_argument("pin")

    return parser


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

    try:
        if args.command == "start":
            if not args.device_id and not args.address:
                parser.error("start requires a device id or --address")
            device_id = args.device_id
            if not device_id:
                device_id, code = _resolve_by_address(args)
                if device_id is None:
                    return code
            response = asyncio.run(request("start", device_id=device_id))
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
