"""Prototype of omarchy-cast Tasks 10+11: portal ScreenCast -> PipeWire -> VAAPI -> appsink.

Validates the exact path the Cast backend will use: the pipeline ends in an
appsink and Python receives the encoded chunks. Here we write them to a file
instead of pushing them to an HTTP socket.

Usage: python3 capture_test.py [seconds] [encoder]
"""

import os
import sys
import time

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 6
ENCODER = sys.argv[2] if len(sys.argv) > 2 else "vaapi"
OUT = "/tmp/claude-1000/-home-mrcode-workspace-omarchy/a5b69c65-ed54-434c-9997-945d907c7cf5/scratchpad/capture_out.mkv"

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.ScreenCast"

ENCODER_ELEMENTS = {"vaapi": "vah264enc", "nvenc": "nvh264enc", "x264": "x264enc"}
ENCODER_ARGS = {
    "vaapi": "rate-control=cbr bitrate=8000 target-usage=6 key-int-max=30",
    "nvenc": "rc-mode=cbr bitrate=8000",
    "x264": "tune=zerolatency speed-preset=veryfast bitrate=8000 key-int-max=30",
}

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
proxy = Gio.DBusProxy.new_sync(
    bus, Gio.DBusProxyFlags.NONE, None, BUS_NAME, OBJECT_PATH, IFACE, None
)
loop = GLib.MainLoop()

TOKEN_FILE = "/tmp/claude-1000/-home-mrcode-workspace-omarchy/a5b69c65-ed54-434c-9997-945d907c7cf5/scratchpad/restore_token"


def load_token():
    try:
        with open(TOKEN_FILE) as fh:
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def save_token(token):
    with open(TOKEN_FILE, "w") as fh:
        fh.write(token)


state = {"step": 0, "session": None, "node_id": None, "fd": None, "error": None}
stats = {"chunks": 0, "bytes": 0, "first_chunk_at": None}
started = time.monotonic()


def fail(msg):
    state["error"] = msg
    loop.quit()


def on_response(_conn, _sender, _path, _iface, _signal, params):
    code, payload = params.unpack()
    step = state["step"]

    if code != 0:
        fail(f"portal denied/cancelled at step {step} (response code {code})")
        return

    try:
        if step == 0:
            state["session"] = payload["session_handle"]
            print(f"  [1/4] session created: {state['session']}")
            state["step"] = 1
            opts = {
                "types": GLib.Variant("u", 1),          # MONITOR
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", 2),    # EMBEDDED
                "persist_mode": GLib.Variant("u", 2),   # PERSISTENT
            }
            stored = load_token()
            if stored:
                opts["restore_token"] = GLib.Variant("s", stored)
                print(f"  [2/4] SelectSources with stored restore_token "
                      f"-- SHOULD NOT PROMPT")
            else:
                print("  [2/4] SelectSources (no stored token) "
                      "-- A PORTAL DIALOG SHOULD APPEAR NOW")
            proxy.call_sync(
                "SelectSources",
                GLib.Variant("(oa{sv})", (state["session"], opts)),
                Gio.DBusCallFlags.NONE, -1, None,
            )
        elif step == 1:
            print("  [3/4] sources selected, starting...")
            state["step"] = 2
            proxy.call_sync(
                "Start",
                GLib.Variant("(osa{sv})", (state["session"], "", {})),
                Gio.DBusCallFlags.NONE, -1, None,
            )
        elif step == 2:
            streams = payload.get("streams") or []
            if not streams:
                fail("portal returned no streams")
                return
            state["node_id"] = int(streams[0][0])
            props = streams[0][1]
            size = props.get("size")
            print(f"  [4/4] stream node_id={state['node_id']} size={size}")
            if "restore_token" in payload:
                save_token(payload["restore_token"])
                print(f"        restore_token saved (len {len(payload['restore_token'])})")
            else:
                print("        NO restore_token -- will re-prompt next time")
            loop.quit()
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}")


bus.signal_subscribe(
    BUS_NAME, "org.freedesktop.portal.Request", "Response",
    None, None, Gio.DBusSignalFlags.NONE, on_response,
)

print("== portal handshake ==")
proxy.call_sync(
    "CreateSession",
    GLib.Variant("(a{sv})", ({"session_handle_token": GLib.Variant("s", "omarchycast")},)),
    Gio.DBusCallFlags.NONE, -1, None,
)

GLib.timeout_add_seconds(120, lambda: (fail("timed out waiting for portal"), False)[1])
loop.run()

if state["error"]:
    print(f"\nFAILED: {state['error']}")
    sys.exit(1)

# Get the PipeWire fd
variant, fd_list = proxy.call_with_unix_fd_list_sync(
    "OpenPipeWireRemote",
    GLib.Variant("(oa{sv})", (state["session"], {})),
    Gio.DBusCallFlags.NONE, -1, None, None,
)
state["fd"] = fd_list.steal_fds()[0]
print(f"        pipewire fd={state['fd']}")

# Build the pipeline exactly as the plan specifies, ending in appsink.
element = ENCODER_ELEMENTS[ENCODER]
args = ENCODER_ARGS[ENCODER]
desc = (
    f"pipewiresrc path={state['node_id']} fd={state['fd']} do-timestamp=true ! "
    f"videorate ! video/x-raw,framerate=30/1 ! "
    f"videoconvert ! "
    f"{element} {args} ! "
    f"h264parse config-interval=1 ! "
    f"matroskamux streamable=true ! "
    f"appsink emit-signals=true sync=false max-buffers=4 drop=true name=sink"
)
print(f"\n== pipeline ==\n{desc}\n")

Gst.init(None)
pipeline = Gst.parse_launch(desc)
out = open(OUT, "wb")


def on_sample(sink):
    sample = sink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if ok:
        try:
            data = bytes(info.data)
            out.write(data)
            stats["chunks"] += 1
            stats["bytes"] += len(data)
            if stats["first_chunk_at"] is None:
                stats["first_chunk_at"] = time.monotonic() - started
        finally:
            buf.unmap(info)
    return Gst.FlowReturn.OK


sink = pipeline.get_by_name("sink")
sink.connect("new-sample", on_sample)


def on_bus(_bus, message):
    if message.type == Gst.MessageType.ERROR:
        err, dbg = message.parse_error()
        fail(f"GStreamer: {err.message} | {dbg}")
    elif message.type == Gst.MessageType.EOS:
        loop.quit()
    return True


pipeline.get_bus().add_signal_watch()
pipeline.get_bus().connect("message", on_bus)

pipeline.set_state(Gst.State.PLAYING)
print(f"== capturing for {DURATION}s with {ENCODER} ({element}) ==")

loop2 = GLib.MainLoop()
loop = loop2
GLib.timeout_add_seconds(DURATION, lambda: (loop2.quit(), False)[1])
loop2.run()

pipeline.set_state(Gst.State.NULL)
out.close()

print()
if state["error"]:
    print(f"FAILED: {state['error']}")
    sys.exit(1)

size = os.path.getsize(OUT)
elapsed = time.monotonic() - started
print("== RESULT ==")
print(f"  chunks from appsink : {stats['chunks']}")
print(f"  bytes               : {stats['bytes']:,}")
print(f"  file size           : {size:,}")
print(f"  time to first chunk : {stats['first_chunk_at']:.2f}s" if stats["first_chunk_at"] else "  NO CHUNKS RECEIVED")
print(f"  avg bitrate         : {(stats['bytes'] * 8 / DURATION / 1_000_000):.2f} Mbps")
print(f"  output              : {OUT}")
sys.exit(0 if stats["chunks"] > 0 else 1)
