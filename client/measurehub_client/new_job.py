"""New job dialog: method, instrument, sample and a parameter form built from the JSON Schema."""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from measurehub_client.controller import Controller, describe_error
from measurehub_client.qt_util import set_button_text
from measurehub_client.schema_form import SchemaForm


class NewJobDialog(QDialog):
    def __init__(
        self,
        controller: Controller,
        methods: list[dict[str, Any]],
        instruments: list[dict[str, Any]],
        samples: list[dict[str, Any]],
        schema: dict[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Новое задание")
        self.setMinimumWidth(420)
        self._ctl = controller
        self._methods = methods
        self.created_job: dict[str, Any] | None = None

        self.method = QComboBox()
        self.method.setObjectName("method")
        for item in methods:
            self.method.addItem(f"{item['name']} ({item['processor']})", item["id"])
        self.instrument = QComboBox()
        self.instrument.setObjectName("instrument")
        for item in instruments:
            if item["is_enabled"]:
                self.instrument.addItem(item["name"], item["id"])
        self.sample = QComboBox()
        self.sample.setObjectName("sample")
        self.sample.addItem("— без образца —", None)
        for item in samples:
            self.sample.addItem(
                f"{item['code']} · {item['material']}, {item['thickness_mm']:g} мм", item["id"]
            )
        self.form = SchemaForm()
        self.form.set_schema(schema)
        self.status = QLabel()
        self.status.setObjectName("new_job_status")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #c0392b")
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        set_button_text(self.buttons, QDialogButtonBox.StandardButton.Ok, "Запустить")
        set_button_text(self.buttons, QDialogButtonBox.StandardButton.Cancel, "Отмена")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        top = QFormLayout()
        top.addRow("Методика", self.method)
        top.addRow("Прибор", self.instrument)
        top.addRow("Образец", self.sample)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(QLabel("Параметры измерения"))
        layout.addWidget(self.form)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self.method.currentIndexChanged.connect(self._method_changed)
        self._method_changed()

    def _current_method(self) -> dict[str, Any] | None:
        index = self.method.currentIndex()
        return self._methods[index] if 0 <= index < len(self._methods) else None

    def _method_changed(self) -> None:
        method = self._current_method()
        if method is not None:
            self.form.set_values(method["params"])

    def overrides(self) -> dict[str, Any]:
        """Only the parameters that differ from the method's own."""
        method = self._current_method()
        base = method["params"] if method else {}
        return {k: v for k, v in self.form.values().items() if base.get(k) != v}

    def _submit(self) -> None:
        method_id = self.method.currentData()
        instrument_id = self.instrument.currentData()
        if method_id is None or instrument_id is None:
            self.status.setText("Выберите методику и прибор")
            return
        self.status.clear()
        self.buttons.setEnabled(False)
        sample_id = self.sample.currentData()
        overrides = self.overrides()
        api = self._ctl.api
        self._ctl.run(
            lambda: api.create_job(method_id, instrument_id, sample_id, overrides),
            self._created,
            self._failed,
        )

    def _created(self, job: dict[str, Any]) -> None:
        self.created_job = job
        self.accept()

    def _failed(self, exc: Exception) -> None:
        self.buttons.setEnabled(True)
        self.status.setText(describe_error(exc))
