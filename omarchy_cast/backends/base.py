from abc import ABC, abstractmethod
from collections.abc import Callable

from omarchy_cast.backends.creds import MIRROR
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState

StateCallback = Callable[[Device, SessionState, str | None], None]


class BackendError(Exception):
    """Raised when a backend cannot start, stop, or sustain a session.

    The message is shown directly to the user, so it must be actionable.
    """


class Backend(ABC):
    """A transport for one protocol.

    The daemon must not be able to tell whether a backend brings its own screen
    capture (AirPlay, where doubletake captures for itself) or consumes one the
    daemon owns (Cast). That is the seam that keeps a shared capture core a
    backend swap rather than a rewrite.
    """

    protocol: str = ""

    def __init__(self, on_state: StateCallback) -> None:
        self._on_state = on_state

    def _emit(self, device: Device, state: SessionState, error: str | None = None) -> None:
        self._on_state(device, state, error)

    @abstractmethod
    async def start(self, device: Device, mode: str = MIRROR) -> None: ...

    @abstractmethod
    async def stop(self, device: Device) -> None: ...

    async def submit_pin(self, device: Device, pin: str) -> None:
        raise BackendError(f"{self.protocol} does not use PIN pairing")

    async def shutdown(self) -> None:
        return None
