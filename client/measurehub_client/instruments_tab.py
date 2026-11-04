"""Instruments with statuses updated in real time (MH-49, FR-35)."""

from __future__ import annotations

from typing import Any

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from measurehub_client import format as fmt
from measurehub_client.controller import Controller
from measurehub_client.qt_util import stretch_last_column

COLUMNS = ["Прибор", "Адрес", "Статус", "Идентификация", "Последний ответ"]
STATUS_COLUMN = 2


class InstrumentsTab(QWidget):
    def __init__(self, controller: Controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl = controller
        self._rows: dict[str, int] = {}
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("instruments_table")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        stretch_last_column(self.table)
        self.refresh_button = QPushButton("Обновить")
        self.check_button = QPushButton("Проверить связь")
        self.refresh_button.clicked.connect(self.reload)
        self.check_button.clicked.connect(self.check_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.check_button)
        buttons.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table)

    # -- data --------------------------------------------------------------------------------

    def reload(self) -> None:
        api = self._ctl.api
        self._ctl.run(api.list_instruments, self.set_instruments)

    def set_instruments(self, instruments: list[dict[str, Any]]) -> None:
        self.table.setRowCount(len(instruments))
        self._rows = {}
        for row, item in enumerate(instruments):
            self._rows[item["id"]] = row
            self.table.setItem(row, 0, self._item(item["name"], item["id"]))
            self.table.setItem(row, 1, self._item(item["resource"]))
            self.table.setItem(row, 2, self._status_item(item["status"]))
            self.table.setItem(row, 3, self._item(item["idn"] or "—"))
            self.table.setItem(row, 4, self._item(fmt.local_time(item["last_seen_at"])))

    def apply_event(self, event: dict[str, Any]) -> None:
        """``instrument`` events and the ``snapshot`` of ``/ws/events``."""
        if event["type"] == "instrument":
            self._set_status(event["instrument_id"], event["status"])
        elif event["type"] == "snapshot":
            for item in event.get("instruments", []):
                self._set_status(item["instrument_id"], item["status"])

    def _set_status(self, instrument_id: str, status: str) -> None:
        row = self._rows.get(instrument_id)
        if row is not None:
            self.table.setItem(row, STATUS_COLUMN, self._status_item(status))

    def status_of(self, instrument_id: str) -> str | None:
        row = self._rows.get(instrument_id)
        if row is None:
            return None
        item = self.table.item(row, STATUS_COLUMN)
        return str(item.data(256)) if item else None

    def check_selected(self) -> None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        instrument_id = str(item.data(256))
        api = self._ctl.api
        self._ctl.run(lambda: api.check_instrument(instrument_id), self._checked)

    def _checked(self, instrument: dict[str, Any]) -> None:
        row = self._rows.get(instrument["id"])
        if row is not None:
            self.table.setItem(row, STATUS_COLUMN, self._status_item(instrument["status"]))
            self.table.setItem(row, 3, self._item(instrument["idn"] or "—"))
            self.table.setItem(row, 4, self._item(fmt.local_time(instrument["last_seen_at"])))

    # -- cells -------------------------------------------------------------------------------

    @staticmethod
    def _item(text: str, key: str | None = None) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if key is not None:
            item.setData(256, key)  # Qt.ItemDataRole.UserRole
        return item

    @staticmethod
    def _status_item(status: str) -> QTableWidgetItem:
        item = QTableWidgetItem(fmt.instrument_status_text(status))
        item.setData(256, status)
        item.setForeground(QColor(fmt.COLORS.get(status, "#000000")))
        return item
