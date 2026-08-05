import asyncio
import contextlib
import logging
import socket

log = logging.getLogger(__name__)

STREAM_PATH = "/stream.mkv"
CONTENT_TYPE = "video/x-matroska"


def format_headers(content_type: str) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Cache-Control: no-cache, no-store\r\n"
        "Connection: close\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "\r\n"
    ).encode("ascii")


def local_address_for(target: str) -> str:
    """The local IPv4 address the kernel would use to reach `target`.

    Binding 0.0.0.0 and guessing is not good enough: this machine has several
    docker bridges, and advertising the wrong one to a receiver silently fails.
    The socket is connectionless, so nothing is actually sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


class StreamServer:
    """Single-endpoint HTTP server pushing live chunks to connected clients."""

    url_path = STREAM_PATH

    def __init__(self, host: str, port: int = 0) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def client_count(self) -> int:
        return len(self._writers)

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def push(self, chunk: bytes) -> None:
        """Fan a chunk out to every client. Safe to call with no clients."""
        if not chunk:
            return
        framed = b"%x\r\n%s\r\n" % (len(chunk), chunk)
        for writer in list(self._writers):
            if writer.is_closing():
                self._writers.discard(writer)
                continue
            try:
                writer.write(framed)
            except Exception:
                self._writers.discard(writer)

    async def _on_client(self, reader, writer) -> None:
        try:
            request_line = await reader.readline()
            while True:
                header = await reader.readline()
                if header in (b"\r\n", b"\n", b""):
                    break

            if STREAM_PATH.encode() not in request_line:
                writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            writer.write(format_headers(CONTENT_TYPE))
            await writer.drain()
            self._writers.add(writer)

            while not writer.is_closing():
                await asyncio.sleep(0.2)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
