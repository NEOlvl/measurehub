"""Result card: eps and mu graphs and CSV/JSON export (MH-53, FR-39)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from measurehub_client.controller import Controller
from measurehub_client.plots import add_curve, make_plot, set_curve

SUMMARY_TEXT = (
    ("eps_re_mean", "ε′"),
    ("eps_im_mean", "ε″"),
    ("mu_re_mean", "μ′"),
    ("mu_im_mean", "μ″"),
)


def to_array(values: list[float | None]) -> NDArray[np.float64]:
    """Result columns come as JSON: ``null`` stands for a point that could not be computed."""
    return np.array([np.nan if v is None else v for v in values], dtype=np.float64)


class ResultCard(QWidget):
    def __init__(self, controller: Controller, job_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctl = controller
        self.job_id = job_id
        self.result: dict[str, Any] | None = None

        self.summary = QLabel("Загрузка результата…")
        self.summary.setObjectName("summary")
        self.summary.setStyleSheet("font-size: 13px")
        self.quality = QLabel()
        self.quality.setObjectName("quality")
        self.status = QLabel()
        self.status.setObjectName("export_status")
        self.eps_plot = make_plot("Диэлектрическая проницаемость ε", "ε")
        self.mu_plot = make_plot("Магнитная проницаемость μ", "μ")
        self.curves = {
            "eps_re": add_curve(self.eps_plot, "ε′", 0),
            "eps_im": add_curve(self.eps_plot, "ε″", 1),
            "mu_re": add_curve(self.mu_plot, "μ′", 2),
            "mu_im": add_curve(self.mu_plot, "μ″", 3),
        }
        self.csv_button = QPushButton("Выгрузить CSV")
        self.csv_button.setObjectName("export_csv")
        self.json_button = QPushButton("Выгрузить JSON")
        self.json_button.setObjectName("export_json")
        self.raw_button = QPushButton("Сырые данные CSV")
        self.raw_button.setObjectName("export_raw")
        self.csv_button.clicked.connect(lambda: self.export("csv", "result"))
        self.json_button.clicked.connect(lambda: self.export("json", "result"))
        self.raw_button.clicked.connect(lambda: self.export("csv", "raw"))

        plots = QHBoxLayout()
        plots.addWidget(self.eps_plot)
        plots.addWidget(self.mu_plot)
        buttons = QHBoxLayout()
        buttons.addWidget(self.csv_button)
        buttons.addWidget(self.json_button)
        buttons.addWidget(self.raw_button)
        buttons.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.quality)
        layout.addLayout(plots, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

    def load(self) -> None:
        api = self._ctl.api
        self._ctl.run(lambda: api.job_result(self.job_id), self._loaded)

    def _loaded(self, result: dict[str, Any]) -> None:
        self.result = result
        data = result["data"]
        x = to_array(data["freq_hz"]) / 1e9
        for name, curve in self.curves.items():
            set_curve(curve, x, to_array(data["columns"][name]))
        summary = result["summary"]
        if all(summary.get(key) is not None for key, _ in SUMMARY_TEXT):
            self.summary.setText(
                "Средние по диапазону:   "
                + "    ".join(f"{label} = {summary[key]:.4g}" for key, label in SUMMARY_TEXT)
            )
        else:
            self.summary.setText("Среднее по диапазону не определено")
        flagged = summary.get("flagged_fraction") or 0.0
        self.quality.setText(
            "Качество: все точки в порядке"
            if flagged == 0
            else f"Качество: {flagged:.1%} точек помечены (фаза неоднозначна или данные вырождены)"
        )

    # -- export ------------------------------------------------------------------------------

    def default_name(self, fmt_: str, kind: str) -> str:
        return f"job-{self.job_id[:8]}-{kind}.{fmt_}"

    def export(self, fmt_: str, kind: str) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить файл",
            self.default_name(fmt_, kind),
            "CSV (*.csv)" if fmt_ == "csv" else "JSON (*.json)",
        )
        if not path:
            return
        api = self._ctl.api
        self._ctl.run(
            lambda: api.export_job(self.job_id, fmt_, kind), lambda data: self._save(path, data)
        )

    def _save(self, path: str, data: bytes) -> None:
        Path(path).write_bytes(data)
        self.status.setText(f"Сохранено: {path} ({len(data)} байт)")
