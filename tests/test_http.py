import asyncio

from omarchy_cast.capture.http import StreamServer, format_headers, local_address_for


def test_headers_are_chunked_and_typed():
    headers = format_headers("video/x-matroska").decode()
    assert "HTTP/1.1 200 OK" in headers
    assert "Content-Type: video/x-matroska" in headers
    assert "Transfer-Encoding: chunked" in headers
    assert headers.endswith("\r\n\r\n")


def test_headers_disable_caching():
    """Receivers will happily cache a live stream and play it back stale."""
    assert "no-cache" in format_headers("video/x-matroska").decode()


def test_local_address_for_returns_ipv4():
    address = local_address_for("192.168.1.50")
    assert address.count(".") == 3
    assert address != "0.0.0.0"


async def test_server_binds_ephemeral_port_and_reports_it():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        assert port > 0
    finally:
        await server.stop()


async def connect_and_read_headers(port, path="/stream.mkv"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    header = await reader.readuntil(b"\r\n\r\n")
    return reader, writer, header


async def test_client_receives_pushed_chunks():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        reader, writer, header = await connect_and_read_headers(port)
        assert b"200 OK" in header

        await asyncio.sleep(0.05)
        server.push(b"abc")

        assert (await reader.readline()).strip() == b"3"
        assert await reader.readexactly(3) == b"abc"
        writer.close()
    finally:
        await server.stop()


async def test_client_count_tracks_connections():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        assert server.client_count == 0
        reader, writer, _ = await connect_and_read_headers(port)
        assert server.client_count == 1
        writer.close()
    finally:
        await server.stop()


async def test_unknown_path_returns_404():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        assert b"404" in await reader.readline()
        writer.close()
    finally:
        await server.stop()


async def test_push_with_no_clients_is_harmless():
    server = StreamServer("127.0.0.1", 0)
    await server.start()
    try:
        server.push(b"data")
    finally:
        await server.stop()


async def test_empty_push_is_ignored():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        reader, writer, _ = await connect_and_read_headers(port)
        server.push(b"")
        server.push(b"xy")
        assert (await reader.readline()).strip() == b"2"
        writer.close()
    finally:
        await server.stop()


async def test_two_clients_both_receive():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    try:
        r1, w1, _ = await connect_and_read_headers(port)
        r2, w2, _ = await connect_and_read_headers(port)
        await asyncio.sleep(0.05)
        server.push(b"hi")
        for r in (r1, r2):
            assert (await r.readline()).strip() == b"2"
            assert await r.readexactly(2) == b"hi"
        w1.close()
        w2.close()
    finally:
        await server.stop()


async def test_stop_closes_clients():
    server = StreamServer("127.0.0.1", 0)
    port = await server.start()
    reader, writer, _ = await connect_and_read_headers(port)
    await server.stop()
    assert server.client_count == 0
