import pytest

from omarchy_cast.core.protocol import decode_line, encode_request, err, ok


def test_roundtrip():
    line = encode_request("start", device_id="cast:1")
    assert decode_line(line) == {"cmd": "start", "device_id": "cast:1"}


def test_encode_ends_with_newline():
    assert encode_request("list").endswith(b"\n")


def test_none_values_are_dropped():
    """`stop` with no device must not send device_id: null."""
    assert decode_line(encode_request("stop", device_id=None)) == {"cmd": "stop"}


def test_decode_rejects_non_object():
    with pytest.raises(ValueError, match="object"):
        decode_line(b'["nope"]\n')


def test_decode_rejects_missing_cmd():
    with pytest.raises(ValueError, match="cmd"):
        decode_line(b'{"device_id": "x"}\n')


def test_ok_and_err_shapes():
    assert ok({"a": 1}) == {"ok": True, "data": {"a": 1}}
    assert err("bad") == {"ok": False, "error": "bad"}


def test_socket_path_uses_xdg_runtime_dir(monkeypatch):
    from omarchy_cast.core.protocol import socket_path

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/9999")
    assert str(socket_path()) == "/run/user/9999/omarchy-cast.sock"
