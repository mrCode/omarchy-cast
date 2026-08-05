import asyncio
import json

import pytest

from omarchy_cast.cli.client import DaemonUnavailable, request


async def run_echo_server(path, response, capture=None):
    async def handle(reader, writer):
        line = await reader.readline()
        if capture is not None:
            capture.append(json.loads(line))
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


async def test_request_roundtrip(tmp_path):
    sock = tmp_path / "test.sock"
    server = await run_echo_server(sock, {"ok": True, "data": {"devices": []}})
    try:
        resp = await request("list", path=sock)
        assert resp == {"ok": True, "data": {"devices": []}}
    finally:
        server.close()
        await server.wait_closed()


async def test_request_sends_kwargs(tmp_path):
    sock = tmp_path / "test.sock"
    seen = []
    server = await run_echo_server(sock, {"ok": True, "data": {}}, capture=seen)
    try:
        await request("start", path=sock, device_id="cast:1")
        assert seen[0] == {"cmd": "start", "device_id": "cast:1"}
    finally:
        server.close()
        await server.wait_closed()


async def test_missing_socket_without_autospawn_raises(tmp_path):
    with pytest.raises(DaemonUnavailable):
        await request("list", path=tmp_path / "nope.sock", autospawn=False)


async def test_daemon_closing_without_reply_is_actionable(tmp_path):
    sock = tmp_path / "quiet.sock"

    async def handle(reader, writer):
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock))
    try:
        with pytest.raises(DaemonUnavailable, match="without responding"):
            await request("list", path=sock)
    finally:
        server.close()
        await server.wait_closed()


async def test_stale_socket_is_replaced_not_fatal(tmp_path, monkeypatch):
    """A daemon that dies without cleaning up leaves the socket behind. The
    existence check then skips the spawn, and every command fails with
    'connection refused' until someone removes the file by hand.
    """
    from omarchy_cast.cli import client

    sock = tmp_path / "stale.sock"
    sock.write_text("")          # a file, but nothing listening
    spawned = []

    def fake_spawn():
        spawned.append(True)

        async def serve():
            await run_echo_server(sock, {"ok": True, "data": {"respawned": True}})

        asyncio.get_running_loop().create_task(serve())

    monkeypatch.setattr(client, "_spawn_daemon", fake_spawn)
    resp = await request("status", path=sock)
    assert spawned, "should have respawned the daemon"
    assert resp["data"]["respawned"] is True


async def test_stale_socket_without_autospawn_still_raises(tmp_path):
    sock = tmp_path / "stale2.sock"
    sock.write_text("")
    with pytest.raises(DaemonUnavailable):
        await request("status", path=sock, autospawn=False)
