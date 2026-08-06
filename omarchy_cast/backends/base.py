from abc import ABC, abstractmethod
from collections.abc import Callable

from omarchy_cast.core.device import Device
from omarchy_cast.core.session import MIRROR, SessionState

StateCallback = Callable[[Device, SessionState, str | None], None]


class BackendError(Exception):
    """Raised when a backend cannot start, stop, or sustain a session.

    The message is shown directly to the user, so it must be actionable.
    """


class BackendRefused(BackendError):
    """Raised when a backend declines a start WITHOUT touching the device.

    The distinction matters to the daemon and nowhere else. `_cmd_start`
    displaces any existing session record before calling the backend, so on
    failure it must decide whether that record is still true. It is true
    exactly when the backend never got as far as the device: extend asked of a
    Chromecast, or extend asked while another device holds the virtual output.
    Those leave a running cast running.

    Every other failure means the backend already tore the old session down on
    its way in, so the displaced record describes something that no longer
    exists. Restoring it indiscriminately left a green "streaming" indicator on
    the bar for a cast that had just failed to restart.
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
