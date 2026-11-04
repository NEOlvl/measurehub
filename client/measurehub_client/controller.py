"""Glue between the widgets and the server: background calls, session and the global stream.

Every blocking call (HTTP) runs in a ``QThreadPool`` thread, results come back to the GUI
thread through queued signals, so the interface never freezes (MH-48, risk table of the spec).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from measurehub_client.api import ApiClient, ApiError, ConnectionFailedError, SessionExpiredError
from measurehub_client.events import EventStream


class _Signals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:
            self._signals.error.emit(exc)
        else:
            self._signals.result.emit(result)


class Controller(QObject):
    """Owns the API client, the thread pool and the global event stream."""

    session_expired = pyqtSignal()
    error_occurred = pyqtSignal(str)
    global_event = pyqtSignal(dict)
    global_reconnected = pyqtSignal()
    global_state = pyqtSignal(str)  # connecting / connected / reconnecting / closed
    # a job was created or opened from somewhere: the main window shows its card
    open_job_requested = pyqtSignal(str)

    def __init__(self, api: ApiClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._alive: set[_Signals] = set()
        self._global: EventStream | None = None

    # -- background calls --------------------------------------------------------------------

    def run(
        self,
        fn: Callable[[], Any],
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run ``fn`` in a pool thread; the callbacks run in the GUI thread.

        Without ``on_error`` the error is reported to the user (:attr:`error_occurred`).
        An expired session always triggers :attr:`session_expired`.
        """
        signals = _Signals()
        self._alive.add(signals)

        def finish() -> None:
            self._alive.discard(signals)

        def handle_result(result: Any) -> None:
            finish()
            if on_result is not None:
                on_result(result)

        def handle_error(exc: Exception) -> None:
            finish()
            if isinstance(exc, SessionExpiredError):
                self.session_expired.emit()
            elif on_error is not None:
                on_error(exc)
            else:
                self.report(exc)

        signals.result.connect(handle_result)
        signals.error.connect(handle_error)
        self._pool.start(_Task(fn, signals))

    def wait_for_done(self, timeout_ms: int = 5000) -> bool:
        return self._pool.waitForDone(timeout_ms)

    def report(self, exc: Exception) -> None:
        self.error_occurred.emit(describe_error(exc))

    # -- streams -----------------------------------------------------------------------------

    def start_global_stream(self) -> None:
        """Subscribe to ``/ws/events`` (statuses of instruments and jobs)."""
        if self._global is not None:
            return
        stream = EventStream(lambda: self.api.ws_url("/ws/events"), self)
        stream.message.connect(self.global_event)
        stream.reconnected.connect(self.global_reconnected)
        stream.state_changed.connect(self.global_state)
        stream.unauthorized.connect(self.session_expired)
        self._global = stream
        stream.start()

    def restart_global_stream(self) -> None:
        """Reconnect the global stream (after a new login: the token changed)."""
        if self._global is not None:
            self._global.stop()
            self._global = None
        self.start_global_stream()

    def job_stream(self, job_id: str) -> EventStream:
        """A new stream for ``/ws/jobs/{id}``; the caller starts and stops it."""
        stream = EventStream(lambda: self.api.ws_url(f"/ws/jobs/{job_id}"), self)
        stream.unauthorized.connect(self.session_expired)
        return stream

    def shutdown(self) -> None:
        if self._global is not None:
            self._global.stop()
            self._global = None
        self.wait_for_done()


def describe_error(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return exc.message()
    if isinstance(exc, ConnectionFailedError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
