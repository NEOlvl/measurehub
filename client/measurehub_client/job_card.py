"""Job card: progress, status, log, live |S11| and |S21| graphs and cancel (MH-52, FR-38)."""

from __future__ import annotations

import html
from typing import Any

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from measurehub_client import format as fmt
from measurehub_client.buffer import SweepBuffer
from measurehub_client.controller import Controller
from measurehub_client.events import EventStream
from measurehub_client.plots import add_curve, make_plot, set_curve


class JobCard(QWidget):
    result_requested = pyqtSignal(str)

    def __init__(
        self,
        controller: Controller,
        job_id: str,
        title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._ctl = controller
        self.job_id = job_id
        self.job: dict[str, Any] | None = None
        self.buffer: SweepBuffer | None = None
        self.stream: EventStream | None = None

        self.header = QLabel(title or f"Задание {job_id[:8]}")
        self.header.setObjectName("job_header")
        self.header.setStyleSheet("font-weight: bold; font-size: 14px")
        self.status_label = QLabel("…")
        self.status_label.setObjectName("status_label")
        self.connection_label = QLabel()
        self.connection_label.setObjectName("connection_label")
        self.progress = QProgressBar()
        self.progress.setObjectName("progress")
        self.progress.setRange(0, 100)
        self.error_label = QLabel()
        self.error_label.setObjectName("error_label")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #c0392b")
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setObjectName("cancel_button")
        self.retry_button = QPushButton("Повторить")
        self.retry_button.setObjectName("retry_button")
        self.result_button = QPushButton("Результат")
        self.result_button.setObjectName("result_button")
        for button in (self.cancel_button, self.retry_button, self.result_button):
            button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.retry_button.clicked.connect(self._retry)
        self.result_button.clicked.connect(lambda: self.result_requested.emit(self.job_id))

        self.s11_plot = make_plot("|S11|", "дБ")
        self.s21_plot = make_plot("|S21|", "дБ")
        self.s11_curve = add_curve(self.s11_plot, "|S11|", 0)
        self.s21_curve = add_curve(self.s21_plot, "|S21|", 1)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)

        plots = QWidget()
        plots_layout = QHBoxLayout(plots)
        plots_layout.setContentsMargins(0, 0, 0, 0)
        plots_layout.addWidget(self.s11_plot)
        plots_layout.addWidget(self.s21_plot)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(plots)
        splitter.addWidget(self.log)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        top = QHBoxLayout()
        top.addWidget(self.status_label)
        top.addWidget(self.progress, 1)
        top.addWidget(self.cancel_button)
        top.addWidget(self.retry_button)
        top.addWidget(self.result_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addLayout(top)
        layout.addWidget(self.error_label)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.connection_label)

    # -- loading -----------------------------------------------------------------------------

    def load(self) -> None:
        """Fetch the job, the points measured so far and the log, then follow the live events."""
        self._ctl.run(self._fetch, self._loaded)

    def _fetch(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        api = self._ctl.api
        return api.get_job(self.job_id), api.job_points(self.job_id), api.job_events(self.job_id)

    def _loaded(
        self, data: tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]
    ) -> None:
        job, points, events = data
        self.buffer = SweepBuffer.for_params(job["params"])
        self.buffer.add_points(points)
        self.log.clear()
        for event in events:
            self._log_stored(event)
        self._apply_job(job)
        self._redraw()
        self.start_stream()

    def start_stream(self) -> None:
        if self.stream is None:
            self.stream = self._ctl.job_stream(self.job_id)
            self.stream.message.connect(self._on_event)
            self.stream.state_changed.connect(self._on_state)
            self.stream.reconnected.connect(self._repair)
        self.stream.start()

    def stop_stream(self) -> None:
        if self.stream is not None:
            self.stream.stop()

    def _repair(self) -> None:
        """After a reconnect: fetch what was missed while the connection was down."""
        self._log("warning", "Связь восстановлена, данные обновлены")
        self._ctl.run(self._fetch, self._repaired)

    def _repaired(
        self, data: tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]
    ) -> None:
        job, points, _ = data
        if self.buffer is not None:
            self.buffer.add_points(points)
        self._apply_job(job)
        self._redraw()

    # -- state -------------------------------------------------------------------------------

    @property
    def status(self) -> str:
        return str(self.job["status"]) if self.job else ""

    def _apply_job(self, job: dict[str, Any]) -> None:
        self.job = job
        self._set_status(job["status"])
        self._set_progress(job["points_done"], job["points_total"])
        self.error_label.setText(fmt.error_summary(job.get("error_code"), job.get("error_message")))
        self.header.setText(self.header.text() or f"Задание {self.job_id[:8]}")

    def _set_status(self, status: str) -> None:
        if self.job is not None:
            self.job["status"] = status
        self.status_label.setText(fmt.job_status_text(status))
        self.status_label.setStyleSheet(
            f"color: {fmt.COLORS.get(status, '#000')}; font-weight: bold"
        )
        self.cancel_button.setEnabled(status in fmt.CANCELLABLE)
        self.retry_button.setEnabled(status in {"failed", "cancelled"})
        self.result_button.setEnabled(status == "completed")

    def _set_progress(self, done: int, total: int) -> None:
        self.progress.setValue(fmt.progress_percent(done, total))
        self.progress.setFormat(f"{done} / {total} точек")

    def _redraw(self) -> None:
        if self.buffer is None:
            return
        x = self.buffer.freq_hz / 1e9
        for plot in (self.s11_plot, self.s21_plot):
            plot.setXRange(
                float(x[0]), float(x[-1]), padding=0.02
            )  # the whole sweep, not the part so far
        set_curve(self.s11_curve, x, self.buffer.s11_db)
        set_curve(self.s21_curve, x, self.buffer.s21_db)

    # -- events ------------------------------------------------------------------------------

    def _on_state(self, state: str) -> None:
        text = {
            "connected": "Обновления в реальном времени: подключено",
            "reconnecting": "Связь потеряна, переподключение…",
            "connecting": "Подключение…",
            "closed": "",
        }.get(state, state)
        self.connection_label.setText(text)

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "snapshot":
            if self.job is not None:
                self._set_status(event["status"])
            self._set_progress(event["points_done"], event["points_total"])
        elif kind == "status":
            self._set_status(event["status"])
            self._log("info", f"Статус: {fmt.job_status_text(event['status'])}")
            if event["status"] in fmt.FINISHED:
                self._ctl.run(lambda: self._ctl.api.get_job(self.job_id), self._apply_job)
        elif kind == "progress":
            self._set_progress(event["points_done"], event["points_total"])
        elif kind == "segment":
            if self.buffer is not None:
                self.buffer.add_segment(event["from_idx"], event["s11_db"], event["s21_db"])
                self._redraw()
        elif kind == "log":
            self._log(event["level"], event["message"])
        elif kind == "error":
            self._log("error", f"Ошибка: {fmt.error_summary(event['code'], event['message'])}")

    # -- log ---------------------------------------------------------------------------------

    def _log(self, level: str, text: str, when: str | None = None) -> None:
        color = fmt.LOG_COLORS.get(level, fmt.LOG_COLORS["info"])
        stamp = fmt.local_time(when).split(" ")[-1] if when else _now_text()
        self.log.appendHtml(
            f'<span style="color:{color}">{stamp}&nbsp;&nbsp;{html.escape(text)}</span>'
        )

    def _log_stored(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        if event["event"] == "status":
            text = f"Статус: {fmt.job_status_text(payload['status'])}"
        elif event["event"] == "error":
            text = f"Ошибка: {fmt.error_summary(payload['code'], payload['message'])}"
        else:
            text = str(payload.get("message", event["event"]))
        self._log(event["level"], text, event["ts"])

    def log_text(self) -> str:
        return self.log.toPlainText()

    def curve_points(self) -> int:
        """Number of measured points currently drawn on the |S21| graph."""
        x, y = self.s21_curve.getData()
        return 0 if x is None or y is None else int(np.count_nonzero(np.isfinite(y)))

    # -- actions -----------------------------------------------------------------------------

    def _cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        api = self._ctl.api
        self._ctl.run(lambda: api.cancel_job(self.job_id), self._apply_job, self._action_failed)

    def _retry(self) -> None:
        self.retry_button.setEnabled(False)
        api = self._ctl.api
        self._ctl.run(
            lambda: api.retry_job(self.job_id),
            lambda job: self._ctl.open_job_requested.emit(job["id"]),
            self._action_failed,
        )

    def _action_failed(self, exc: Exception) -> None:
        self._set_status(self.status)
        self._ctl.report(exc)


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")
