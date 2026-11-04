"""Job list with a status filter (MH-51, FR-37)."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from measurehub_client import format as fmt
from measurehub_client.controller import Controller
from measurehub_client.qt_util import stretch_last_column

COLUMNS = ["Создано", "Прибор", "Образец", "Методика", "Статус", "Прогресс"]
ID_ROLE = 256
POLL_MS = 3000


class JobsTab(QWidget):
    job_activated = pyqtSignal(str)  # double click or "Открыть"
    new_job_requested = pyqtSignal()

    def __init__(self, controller: Controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl = controller
        self._names: dict[str, dict[str, str]] = {"instrument": {}, "sample": {}, "method": {}}
        self._jobs: list[dict[str, Any]] = []
        self.filter = QComboBox()
        self.filter.setObjectName("status_filter")
        self.filter.addItem("Все статусы", None)
        for status, text in fmt.JOB_STATUS.items():
            self.filter.addItem(text, status)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("jobs_table")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        stretch_last_column(self.table)
        self.total = QLabel()
        new_button = QPushButton("Новое задание")
        new_button.setObjectName("new_job_button")
        open_button = QPushButton("Открыть")
        refresh_button = QPushButton("Обновить")
        new_button.clicked.connect(self.new_job_requested)
        open_button.clicked.connect(self._open_selected)
        refresh_button.clicked.connect(self.reload)
        self.filter.currentIndexChanged.connect(self.reload)
        self.table.cellDoubleClicked.connect(self._open_row)

        bar = QHBoxLayout()
        bar.addWidget(new_button)
        bar.addWidget(open_button)
        bar.addWidget(refresh_button)
        bar.addStretch()
        bar.addWidget(QLabel("Фильтр:"))
        bar.addWidget(self.filter)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.table)
        layout.addWidget(self.total)

        # running jobs report their progress only to the job card, so the list polls
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def set_names(
        self,
        instruments: list[dict[str, Any]],
        samples: list[dict[str, Any]],
        methods: list[dict[str, Any]],
    ) -> None:
        self._names["instrument"] = {i["id"]: i["name"] for i in instruments}
        self._names["sample"] = {s["id"]: s["code"] for s in samples}
        self._names["method"] = {m["id"]: m["name"] for m in methods}
        self._render()

    def status_filter(self) -> str | None:
        value = self.filter.currentData()
        return None if value is None else str(value)

    def reload(self) -> None:
        api, status = self._ctl.api, self.status_filter()
        self._ctl.run(lambda: api.list_jobs(status), self._loaded)

    def _loaded(self, page: dict[str, Any]) -> None:
        self._jobs = page["items"]
        self.total.setText(f"Всего заданий: {page['total']}")
        self._render()

    def apply_event(self, event: dict[str, Any]) -> None:
        """Status events of ``/ws/events``: update the row, or reload for an unknown job."""
        if event["type"] != "status":
            return
        for job in self._jobs:
            if job["id"] == event["job_id"]:
                job["status"] = event["status"]
                if event["status"] == "completed":
                    job["points_done"] = job["points_total"]
                self._render()
                break
        else:
            self.reload()

    def _poll(self) -> None:
        if any(job["status"] not in fmt.FINISHED for job in self._jobs):
            self.reload()

    def start_polling(self) -> None:
        self._timer.start(POLL_MS)

    def stop_polling(self) -> None:
        self._timer.stop()

    def job_ids(self) -> list[str]:
        return [str(self.table.item(r, 0).data(ID_ROLE)) for r in range(self.table.rowCount())]  # type: ignore[union-attr]

    def _render(self) -> None:
        self.table.setRowCount(len(self._jobs))
        for row, job in enumerate(self._jobs):
            percent = fmt.progress_percent(job["points_done"], job["points_total"])
            created = QTableWidgetItem(fmt.local_time(job["created_at"]))
            created.setData(ID_ROLE, job["id"])
            status = QTableWidgetItem(fmt.job_status_text(job["status"]))
            status.setForeground(QColor(fmt.COLORS.get(job["status"], "#000000")))
            cells = [
                created,
                QTableWidgetItem(self._names["instrument"].get(job["instrument_id"], "?")),
                QTableWidgetItem(self._names["sample"].get(job["sample_id"] or "", "—")),
                QTableWidgetItem(self._names["method"].get(job["method_id"], "?")),
                status,
                QTableWidgetItem(f"{percent}% ({job['points_done']}/{job['points_total']})"),
            ]
            for column, cell in enumerate(cells):
                self.table.setItem(row, column, cell)

    def _open_row(self, row: int, _column: int = 0) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.job_activated.emit(str(item.data(ID_ROLE)))

    def _open_selected(self) -> None:
        if self.table.currentRow() >= 0:
            self._open_row(self.table.currentRow())
