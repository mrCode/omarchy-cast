from omarchy_cast.backends.base import Backend, BackendError, StateCallback
from omarchy_cast.core.device import Device
from omarchy_cast.core.session import SessionState


class StubBackend(Backend):
    """In-memory backend used to test the daemon without hardware.

    Ships in the package rather than the test tree so the daemon tests need no
    fixture imports.
    """

    protocol = "cast"

    def __init__(
        self,
        on_state: StateCallback,
        *,
        fail_with: str | None = None,
        needs_pin: bool = False,
    ) -> None:
        super().__init__(on_state)
        self._fail_with = fail_with
        self._needs_pin = needs_pin

    async def start(self, device: Device) -> None:
        self._emit(device, SessionState.CONNECTING)
        if self._fail_with:
            self._emit(device, SessionState.FAILED, self._fail_with)
            raise BackendError(self._fail_with)
        if self._needs_pin:
            self._emit(device, SessionState.AWAITING_PIN)
            return
        self._emit(device, SessionState.STREAMING)

    async def submit_pin(self, device: Device, pin: str) -> None:
        self._emit(device, SessionState.STREAMING)

    async def stop(self, device: Device) -> None:
        self._emit(device, SessionState.STOPPING)
        self._emit(device, SessionState.IDLE)
