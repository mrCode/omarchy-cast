import time
from enum import StrEnum

from omarchy_cast.core.device import Device

# Cast modes. They live here, next to the Session that carries one, rather than
# in backends/creds.py: the core package must not have to import a backend
# module to know what a mode is.
MIRROR = "mirror"
EXTEND = "extend"
MODES = (MIRROR, EXTEND)


class SessionState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    AWAITING_PIN = "awaiting_pin"
    STREAMING = "streaming"
    STOPPING = "stopping"
    FAILED = "failed"


ALLOWED: dict[SessionState, set[SessionState]] = {
    SessionState.IDLE: {SessionState.CONNECTING},
    SessionState.CONNECTING: {
        SessionState.AWAITING_PIN,
        SessionState.STREAMING,
        SessionState.STOPPING,
    },
    SessionState.AWAITING_PIN: {SessionState.STREAMING, SessionState.STOPPING},
    SessionState.STREAMING: {SessionState.STOPPING},
    SessionState.STOPPING: {SessionState.IDLE},
    SessionState.FAILED: {SessionState.IDLE, SessionState.CONNECTING},
}

ACTIVE = {SessionState.CONNECTING, SessionState.AWAITING_PIN, SessionState.STREAMING}


class InvalidTransition(Exception):
    pass


class Session:
    def __init__(self, device: Device, mode: str = MIRROR) -> None:
        self.device = device
        self.mode = mode
        self.state = SessionState.IDLE
        self.error: str | None = None
        self.started_at: float | None = None

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE

    def transition(self, new: SessionState, error: str | None = None) -> None:
        # FAILED is reachable from anywhere; every other edge must be declared.
        if new is not SessionState.FAILED and new not in ALLOWED[self.state]:
            raise InvalidTransition(f"{self.state} -> {new}")

        if new is SessionState.FAILED:
            self.error = error
        else:
            self.error = None

        if new is SessionState.STREAMING:
            self.started_at = time.monotonic()
        elif new in (SessionState.IDLE, SessionState.FAILED):
            self.started_at = None

        self.state = new
