"""Main window: instruments, jobs, job and result cards in tabs."""

from __future__ import annotations

from typing import Any

from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabBar,
    QTabWidget,
    QWidget,
)

from measurehub_client import format as fmt
from measurehub_client.controller import Controller
from measurehub_client.events import CONNECTED, RECONNECTING
from measurehub_client.instruments_tab import InstrumentsTab
from measurehub_client.job_card import JobCard
from measurehub_client.jobs_tab import JobsTab
from measurehub_client.login import LoginDialog
from measurehub_client.new_job import NewJobDialog
from measurehub_client.qt_util import status_bar
from measurehub_client.result_card import ResultCard


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, username: str = "") -> None:
        super().__init__()
        self.setWindowTitle("MeasureHub")
        self.resize(1200, 760)
        self._ctl = controller
        self.username = username
        self._cards: dict[str, JobCard] = {}
        self._results: dict[str, ResultCard] = {}
        self._lookup: dict[str, list[dict[str, Any]]] = {
            "instruments": [],
            "samples": [],
            "methods": [],
        }

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)
        self.instruments = InstrumentsTab(controller)
        self.jobs = JobsTab(controller)
        self.tabs.addTab(self.instruments, "Приборы")
        self.tabs.addTab(self.jobs, "Задания")
        tab_bar = self.tabs.tabBar()
        assert tab_bar is not None
        for index in (0, 1):  # the two fixed tabs have no close button
            tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, None)

        new_job = QAction("Новое задание", self)
        new_job.setShortcut(QKeySequence("Ctrl+N"))
        new_job.triggered.connect(self.new_job)
        logout = QAction("Сменить пользователя", self)
        logout.triggered.connect(self.relogin)
        toolbar = self.addToolBar("Основное")
        assert toolbar is not None
        toolbar.addAction(new_job)
        toolbar.addAction(logout)

        self.connection = QLabel()
        self.connection.setObjectName("connection")
        status_bar(self).addPermanentWidget(self.connection)
        status_bar(self).showMessage(f"Пользователь: {username}" if username else "")

        self.jobs.job_activated.connect(self.open_job)
        self.jobs.new_job_requested.connect(self.new_job)
        controller.open_job_requested.connect(self.open_job)
        controller.global_event.connect(self._on_global_event)
        controller.global_reconnected.connect(self.refresh)
        controller.global_state.connect(self._on_connection_state)
        controller.session_expired.connect(self.relogin)
        controller.error_occurred.connect(self.show_error)

    # -- startup -----------------------------------------------------------------------------

    def start(self) -> None:
        """Load the lists and subscribe to the live statuses."""
        self.refresh()
        self._on_connection_state("connecting")
        self._ctl.start_global_stream()
        self.jobs.start_polling()

    def refresh(self) -> None:
        api = self._ctl.api

        def load() -> dict[str, list[dict[str, Any]]]:
            return {
                "instruments": api.list_instruments(),
                "samples": api.list_samples(),
                "methods": api.list_methods(),
            }

        self._ctl.run(load, self._loaded)

    def _loaded(self, lookup: dict[str, list[dict[str, Any]]]) -> None:
        self._lookup = lookup
        self.instruments.set_instruments(lookup["instruments"])
        self.jobs.set_names(lookup["instruments"], lookup["samples"], lookup["methods"])
        self.jobs.reload()

    def _on_connection_state(self, state: str) -> None:
        self.connection.setText(
            {
                CONNECTED: "● онлайн",
                RECONNECTING: "● переподключение…",
            }.get(state, "● нет связи")
        )

    # -- events ------------------------------------------------------------------------------

    def _on_global_event(self, event: dict[str, Any]) -> None:
        self.instruments.apply_event(event)
        self.jobs.apply_event(event)

    # -- new job -----------------------------------------------------------------------------

    def new_job(self) -> None:
        api = self._ctl.api

        def load() -> dict[str, Any]:
            return {
                "methods": api.list_methods(),
                "instruments": api.list_instruments(),
                "samples": api.list_samples(),
                "schema": api.measurement_schema("vna_sweep"),
            }

        self._ctl.run(load, self._show_new_job)

    def _show_new_job(self, data: dict[str, Any]) -> None:
        if not data["methods"] or not data["instruments"]:
            self.show_error("Сначала нужны методика и прибор (создаются через API).")
            return
        dialog = NewJobDialog(
            self._ctl, data["methods"], data["instruments"], data["samples"], data["schema"], self
        )
        self.new_job_dialog = dialog
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.created_job is not None:
            self.jobs.reload()
            self.open_job(dialog.created_job["id"])

    # -- cards -------------------------------------------------------------------------------

    def _title(self, job_id: str) -> str:
        return f"Задание {job_id[:8]}"

    def open_job(self, job_id: str) -> JobCard:
        card = self._cards.get(job_id)
        if card is None:
            card = JobCard(self._ctl, job_id, self._title(job_id))
            card.result_requested.connect(self.open_result)
            self._cards[job_id] = card
            self.tabs.addTab(card, self._title(job_id))
            card.load()
        self.tabs.setCurrentWidget(card)
        return card

    def open_result(self, job_id: str) -> ResultCard:
        card = self._results.get(job_id)
        if card is None:
            card = ResultCard(self._ctl, job_id)
            self._results[job_id] = card
            self.tabs.addTab(card, f"Результат {job_id[:8]}")
            card.load()
        self.tabs.setCurrentWidget(card)
        return card

    def _close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, JobCard):
            widget.stop_stream()
            self._cards.pop(widget.job_id, None)
        elif isinstance(widget, ResultCard):
            self._results.pop(widget.job_id, None)
        else:
            return
        self.tabs.removeTab(index)
        widget.deleteLater()

    # -- session and errors ------------------------------------------------------------------

    def relogin(self) -> None:
        """The session expired (or the user switches account): log in again, keep the open cards."""
        if getattr(self, "_relogin_open", False):
            return
        self._relogin_open = True
        try:
            dialog = LoginDialog(self._ctl, self.username, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.close()
                return
            self.username = dialog.username.text().strip()
            status_bar(self).showMessage(f"Пользователь: {self.username}")
            self._ctl.restart_global_stream()
            for card in self._cards.values():
                card.start_stream()
                card.load()
            self.refresh()
        finally:
            self._relogin_open = False

    def show_error(self, text: str) -> None:
        status_bar(self).showMessage(text.splitlines()[0], 10000)
        QMessageBox.warning(self, "MeasureHub", text)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt API)
        self.jobs.stop_polling()
        for card in self._cards.values():
            card.stop_stream()
        self._ctl.shutdown()
        super().closeEvent(event)

    def tab_titles(self) -> list[str]:
        return [self.tabs.tabText(i) for i in range(self.tabs.count())]

    def widget_for_tab(self, index: int) -> QWidget | None:
        return self.tabs.widget(index)

    def job_card(self, job_id: str) -> JobCard | None:
        return self._cards.get(job_id)

    def result_card(self, job_id: str) -> ResultCard | None:
        return self._results.get(job_id)

    def status_text(self, status: str) -> str:
        return fmt.job_status_text(status)
