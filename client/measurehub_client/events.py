"""WebSocket event stream with automatic reconnection (MH-54).

``QWebSocket`` is used in the GUI thread and never blocks it. After a connection drop the
stream reconnects with a growing pause (1, 2, 4, 8, 10, 10 ... s). The server always starts a
connection with a ``snapshot``, so a widget that listens only has to repair what it missed
(it refetches its data when :attr:`EventStream.reconnected` fires).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtWebSockets import QWebSocket

CLOSE_UNAUTHORIZED = 4401
BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 10.0)

CONNECTING = "connecting"
CONNECTED = "connected"
RECONNECTING = "reconnecting"
CLOSED = "closed"


def _close_code(socket: QWebSocket) -> int:
    code: Any = socket.closeCode()
    return int(getattr(code, "value", code))


def reconnect_delay(attempt: int, backoff: Sequence[float] = BACKOFF_S) -> float:
    """Pause before reconnect attempt number ``attempt`` (0-based); the last value repeats."""
    return backoff[min(attempt, len(backoff) - 1)]


class EventStream(QObject):
    message = pyqtSignal(dict)
    state_changed = pyqtSignal(str)
    reconnected = pyqtSignal()
    unauthorized = pyqtSignal()

    def __init__(
        self,
        url_factory: Callable[[], str],
        parent: QObject | None = None,
        backoff: Sequence[float] = BACKOFF_S,
    ) -> None:
        super().__init__(parent)
        self._url_factory = url_factory
        self._backoff = backoff
        self._attempt = 0
        self._had_connection = False
        self._running = False
        self.state = CLOSED
        self._socket = QWebSocket()
        self._socket.connected.connect(self._on_connected)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.textMessageReceived.connect(self._on_text)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._open)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._attempt = 0
        self._set_state(CONNECTING)
        self._open()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        self._socket.close()
        self._set_state(CLOSED)

    # -- internals ---------------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.state_changed.emit(state)

    def _open(self) -> None:
        if self._running:
            self._socket.open(QUrl(self._url_factory()))

    def _on_connected(self) -> None:
        self._attempt = 0
        self._set_state(CONNECTED)
        if self._had_connection:
            self.reconnected.emit()
        self._had_connection = True

    def _on_disconnected(self) -> None:
        if not self._running:
            return
        if _close_code(self._socket) == CLOSE_UNAUTHORIZED:
            self._running = False
            self._set_state(CLOSED)
            self.unauthorized.emit()
            return
        self._set_state(RECONNECTING)
        delay = reconnect_delay(self._attempt, self._backoff)
        self._attempt += 1
        self._timer.start(int(delay * 1000))

    def _on_text(self, text: str) -> None:
        try:
            event: dict[str, Any] = json.loads(text)
        except ValueError:
            return
        self.message.emit(event)
