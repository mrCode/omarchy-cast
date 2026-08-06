"""Textual TUI for omarchy-cast.

A second client of the same daemon socket the CLI uses, so it holds no state of
its own -- everything on screen comes from `list` and `status`.
"""

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
)

from omarchy_cast.core.session import EXTEND, MIRROR
from omarchy_cast.cli.client import DaemonUnavailable, request
from omarchy_cast.tui.model import (
    STATE_STYLE,
    device_row,
    merge,
    session_summary,
    should_prompt_for_pin,
)

REFRESH_SECONDS = 2.0


class PromptScreen(ModalScreen[str | None]):
    """Single-field modal used for both PIN entry and manual addresses."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._title, id="prompt-title")
            yield Input(placeholder=self._placeholder, id="prompt-input")
            with Horizontal(id="prompt-buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#prompt-input", Input).value.strip() or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CastApp(App):
    TITLE = "omarchy-cast"
    CSS = """
    #summary { padding: 1 2; }
    #summary.streaming { color: $success; text-style: bold; }
    #summary.failed { color: $error; text-style: bold; }
    #summary.connecting { color: $warning; }
    DataTable { height: 1fr; }
    #prompt-box {
        width: 60; height: auto; padding: 1 2;
        background: $panel; border: thick $primary;
    }
    #prompt-buttons { height: auto; padding-top: 1; }
    #prompt-buttons Button { margin-right: 2; }
    """

    BINDINGS = [
        Binding("enter", "start", "Start", priority=True),
        Binding("e", "extend", "Extend"),
        Binding("s", "stop", "Stop"),
        Binding("a", "add", "Add by address"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []
        self._pin_pending: str | None = None
        # Start takes several seconds with little visible feedback, so a user
        # will reasonably press Enter again. Without this guard every press
        # opened another portal session and switched the display again --
        # observed as the screen "looping" and five stacked casts.
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Starting...", id="summary")
        yield DataTable(id="devices", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#devices", DataTable)
        table.add_columns("Device", "Protocol", "Model", "State")
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.action_refresh()

    # -- data ------------------------------------------------------------

    # Groups matter: Textual's exclusive=True cancels every worker in the SAME
    # group, and the default group is shared. With all workers in it, the 2s
    # refresh cancelled an in-flight start (which needs ~6s), aborting the cast
    # and bouncing the display resolution. Refresh and user actions must not be
    # able to cancel each other.
    @work(exclusive=True, group="refresh")
    async def action_refresh(self) -> None:
        try:
            devices, status = await asyncio.gather(
                request("list"), request("status")
            )
        except DaemonUnavailable as exc:
            self._set_summary(f"daemon unavailable: {exc}", "failed")
            return

        device_list = (devices.get("data") or {}).get("devices", [])
        sessions = (status.get("data") or {}).get("sessions", [])

        self._rows = merge(device_list, sessions)
        self._render(sessions)

        pending = should_prompt_for_pin(sessions)
        if pending and pending != self._pin_pending:
            self._pin_pending = pending
            self._ask_for_pin(pending)
        elif not pending:
            self._pin_pending = None

    def _render(self, sessions: list[dict]) -> None:
        table = self.query_one("#devices", DataTable)
        cursor = table.cursor_row
        table.clear()
        for row in self._rows:
            name, protocol, model, state = device_row(row)
            style = STATE_STYLE.get(row.get("state", "idle"), "")
            table.add_row(name, protocol, model, f"[{style}]{state}[/]")
        if cursor is not None and cursor < len(self._rows):
            table.move_cursor(row=cursor)

        summary = session_summary(sessions)
        klass = "idle"
        if any(s.get("state") == "failed" for s in sessions):
            klass = "failed"
        elif any(s.get("state") == "streaming" for s in sessions):
            klass = "streaming"
        elif sessions:
            klass = "connecting"
        self._set_summary(summary, klass)

    def _set_summary(self, text: str, klass: str) -> None:
        widget = self.query_one("#summary", Static)
        widget.update(text)
        widget.set_classes(klass)

    def _selected(self) -> dict | None:
        table = self.query_one("#devices", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self._rows):
            return None
        return self._rows[table.cursor_row]

    # -- actions ---------------------------------------------------------

    def action_extend(self) -> None:
        self._start_in_mode(EXTEND)

    def action_start(self) -> None:
        self._start_in_mode(MIRROR)

    def _start_in_mode(self, mode: str) -> None:
        """Synchronous on purpose.

        The guard must be set before any await, otherwise several rapid Enter
        presses all dispatch workers before the first marks us busy -- which is
        exactly how five stacked casts and a looping display happened.
        """
        if self._busy:
            return
        row = self._selected()
        if row is None:
            return
        if row.get("state") in ("connecting", "awaiting_pin", "streaming"):
            self._set_summary(f"{row['name']} is already {row['state']}", "connecting")
            return

        self._busy = True
        if mode == EXTEND:
            # The summary is the only feedback a TUI user gets before the cast
            # is up, so the portal-picker warning has to live here: choosing
            # the wrong output silently mirrors instead of extending, and that
            # choice then repeats on every subsequent extend.
            summary = f"Extending to {row['name']} -- pick 'omarchy-cast' if the portal asks"
        else:
            summary = f"Mirroring to {row['name']} (this takes a few seconds)..."
        self._set_summary(summary, "connecting")
        self._start_worker(row["id"], mode)

    @work(exclusive=True, group="control")
    async def _start_worker(self, device_id: str, mode: str = MIRROR) -> None:
        try:
            await self._send("start", device_id=device_id, mode=mode)
        finally:
            self._busy = False

    def action_stop(self) -> None:
        if self._busy:
            return
        row = self._selected()
        self._busy = True
        self._stop_worker(row["id"] if row else None)

    @work(exclusive=True, group="control")
    async def _stop_worker(self, device_id: str | None) -> None:
        try:
            await self._send("stop", device_id=device_id)
        finally:
            self._busy = False

    @work(exclusive=True, group="control")
    async def action_add(self) -> None:
        address = await self.push_screen_wait(
            PromptScreen("Receiver address", "192.168.1.231")
        )
        if not address:
            return
        protocol = await self.push_screen_wait(
            PromptScreen("Protocol (airplay / cast)", "airplay")
        )
        await self._send("add", address=address, protocol=(protocol or "airplay"))

    @work(exclusive=True, group="control")
    async def _ask_for_pin(self, device_id: str) -> None:
        pin = await self.push_screen_wait(
            PromptScreen("PIN shown on the receiver", "1234")
        )
        if not pin:
            return
        await self._send("pin", device_id=device_id, pin=pin)

    async def _send(self, cmd: str, **kwargs) -> None:
        try:
            response = await request(cmd, **kwargs)
        except DaemonUnavailable as exc:
            self._set_summary(f"daemon unavailable: {exc}", "failed")
            return
        if not response.get("ok"):
            self._set_summary(response.get("error", "unknown error"), "failed")
        self.action_refresh()


def main() -> int:
    CastApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
