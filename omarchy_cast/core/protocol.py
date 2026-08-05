import json
import os
from pathlib import Path
from typing import Any


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "omarchy-cast.sock"


def encode_request(cmd: str, **kwargs: Any) -> bytes:
    payload = {"cmd": cmd, **{k: v for k, v in kwargs.items() if v is not None}}
    return (json.dumps(payload) + "\n").encode("utf-8")


def encode_response(response: dict) -> bytes:
    return (json.dumps(response) + "\n").encode("utf-8")


def decode_line(line: bytes) -> dict:
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    if "cmd" not in data:
        raise ValueError("request missing 'cmd'")
    return data


def ok(data: Any = None) -> dict:
    return {"ok": True, "data": data}


def err(message: str) -> dict:
    return {"ok": False, "error": message}
