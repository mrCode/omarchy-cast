import pytest

from omarchy_cast.capture.portal import (
    PortalError,
    PortalSession,
    load_restore_token,
    parse_streams,
    restore_token_path,
    save_restore_token,
)


def test_parse_streams_extracts_node_id():
    streams = [(42, {"position": (0, 0), "size": (2560, 1600)})]
    assert parse_streams(streams) == 42


def test_parse_streams_takes_first_when_multiple():
    assert parse_streams([(7, {}), (9, {})]) == 7


def test_parse_streams_empty_raises():
    with pytest.raises(PortalError, match="no stream"):
        parse_streams([])


def test_restore_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert load_restore_token() is None
    save_restore_token("tok-123")
    assert load_restore_token() == "tok-123"
    assert restore_token_path().parent.exists()


def test_blank_restore_token_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save_restore_token("   ")
    assert load_restore_token() is None


def test_restore_token_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save_restore_token("tok")
    assert restore_token_path().stat().st_mode & 0o077 == 0


def test_portal_session_carries_fd_and_node():
    s = PortalSession(fd=8, node_id=72)
    assert (s.fd, s.node_id) == (8, 72)
