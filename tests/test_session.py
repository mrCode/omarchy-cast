import pytest

from omarchy_cast.core.device import Device
from omarchy_cast.core.session import InvalidTransition, Session, SessionState


def make_device():
    return Device(id="cast:1", name="TV", address="192.168.1.5", port=8009, protocol="cast")


def test_starts_idle():
    assert Session(make_device()).state is SessionState.IDLE


def test_happy_path_transitions():
    s = Session(make_device())
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.STREAMING)
    s.transition(SessionState.STOPPING)
    s.transition(SessionState.IDLE)
    assert s.state is SessionState.IDLE


def test_pin_path_allowed():
    s = Session(make_device())
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.AWAITING_PIN)
    s.transition(SessionState.STREAMING)
    assert s.state is SessionState.STREAMING


def test_can_fail_from_any_state():
    for start in (SessionState.CONNECTING, SessionState.AWAITING_PIN, SessionState.STREAMING):
        s = Session(make_device())
        s.transition(SessionState.CONNECTING)
        if start is not SessionState.CONNECTING:
            s.transition(start)
        s.transition(SessionState.FAILED, error="boom")
        assert s.state is SessionState.FAILED
        assert s.error == "boom"


def test_illegal_transition_raises():
    s = Session(make_device())
    with pytest.raises(InvalidTransition):
        s.transition(SessionState.STREAMING)


def test_error_cleared_on_leaving_failed():
    s = Session(make_device())
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.FAILED, error="boom")
    s.transition(SessionState.IDLE)
    assert s.error is None


def test_is_active_only_while_connecting_or_streaming():
    s = Session(make_device())
    assert not s.is_active
    s.transition(SessionState.CONNECTING)
    assert s.is_active
    s.transition(SessionState.STREAMING)
    assert s.is_active
    s.transition(SessionState.STOPPING)
    s.transition(SessionState.IDLE)
    assert not s.is_active


def test_started_at_set_on_streaming_and_cleared_on_idle():
    s = Session(make_device())
    assert s.started_at is None
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.STREAMING)
    assert s.started_at is not None
    s.transition(SessionState.STOPPING)
    s.transition(SessionState.IDLE)
    assert s.started_at is None


def test_failed_can_retry_via_connecting():
    s = Session(make_device())
    s.transition(SessionState.CONNECTING)
    s.transition(SessionState.FAILED, error="boom")
    s.transition(SessionState.CONNECTING)
    assert s.state is SessionState.CONNECTING
    assert s.error is None


def test_state_is_a_plain_string_for_json():
    """The daemon serialises state directly into JSON responses."""
    assert str(SessionState.STREAMING) == "streaming"
    assert SessionState.AWAITING_PIN == "awaiting_pin"
