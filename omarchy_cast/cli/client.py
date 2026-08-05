import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from omarchy_cast.core.protocol import encode_request, socket_path

log = logging.getLogger(__name__)

SPAWN_TIMEOUT = 5.0


class DaemonUnavailable(Exception):
    pass


def _spawn_daemon() -> None:
    subprocess.Popen(
        [sys.executable, "-m", "omarchy_cast.core.daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


async def _wait_for_socket(path: Path, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise DaemonUnavailable(f"daemon did not create {path} within {timeout:.0f}s")


async def request(
    cmd: str,
    path: Path | None = None,
    *,
    autospawn: bool = True,
    **kwargs: Any,
) -> dict:
    path = path or socket_path()

    if not path.exists():
        if not autospawn:
            raise DaemonUnavailable(f"no daemon socket at {path}")
        _spawn_daemon()
        await _wait_for_socket(path, SPAWN_TIMEOUT)

    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        # A daemon that died without cleaning up leaves the socket file behind.
        # The existence check above then skips the spawn and every command
        # fails with "connection refused" until someone deletes it by hand.
        if not autospawn:
            raise DaemonUnavailable(f"cannot reach daemon at {path}: {exc}") from exc
        log.debug("stale socket at %s; removing and respawning", path)
        with contextlib.suppress(OSError):
            path.unlink()
        _spawn_daemon()
        await _wait_for_socket(path, SPAWN_TIMEOUT)
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
        except (ConnectionRefusedError, FileNotFoundError) as exc2:
            raise DaemonUnavailable(f"cannot reach daemon at {path}: {exc2}") from exc2

    try:
        writer.write(encode_request(cmd, **kwargs))
        await writer.drain()
        line = await reader.readline()
    except (ConnectionResetError, BrokenPipeError, ConnectionError) as exc:
        # A daemon that dies mid-request resets the socket rather than
        # returning an empty read.
        raise DaemonUnavailable(
            f"daemon closed the connection without responding: {exc}"
        ) from exc
    finally:
        writer.close()

    if not line:
        raise DaemonUnavailable("daemon closed the connection without responding")

    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise DaemonUnavailable(f"malformed response from daemon: {exc}") from exc
