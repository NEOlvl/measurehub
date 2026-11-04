"""Acceptance of phase 4: the whole scenario, from the login to the export, in the client.

Nothing here calls the API directly: the test only does what an operator does in the window
(login, new job, watch it, open the result, export) and looks at what the windows show.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog, QFileDialog, QTableWidget
from pytestqt.qtbot import QtBot

from measurehub_client.api import ApiClient
from measurehub_client.controller import Controller
from measurehub_client.job_card import JobCard
from measurehub_client.login import LoginDialog
from measurehub_client.main_window import MainWindow
from measurehub_client.new_job import NewJobDialog
from measurehub_client.result_card import ResultCard
from tests.client.conftest import LiveServer, Seed
from tests.conftest import PASSWORD

WAIT_MS = 30_000


def cells(table: QTableWidget, row: int) -> list[str]:
    texts = []
    for column in range(table.columnCount()):
        item = table.item(row, column)
        texts.append(item.text() if item else "")
    return texts


def test_operator_scenario_from_login_to_export(
    qtbot: QtBot,
    qapp: object,
    live_server: LiveServer,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    errors: list[str],
) -> None:
    # 1. login ----------------------------------------------------------------------------
    controller = Controller(ApiClient(live_server.url))
    login = LoginDialog(controller)
    qtbot.addWidget(login)
    login.username.setText("alice")
    login.password.setText("wrong")
    login._submit()
    qtbot.waitUntil(lambda: "Wrong username or password" in login.status.text(), timeout=WAIT_MS)
    login.password.setText(PASSWORD)
    with qtbot.waitSignal(login.accepted, timeout=WAIT_MS):
        login._submit()

    window = MainWindow(controller, login.username.text())
    qtbot.addWidget(window)
    window.show()
    window.start()
    try:
        # 2. instruments and jobs lists -----------------------------------------------------
        qtbot.waitUntil(lambda: window.instruments.table.rowCount() == 1, timeout=WAIT_MS)
        assert cells(window.instruments.table, 0)[:3] == [
            "VNA-1",
            f"TCPIP0::127.0.0.1::{seed_port(seed, live_server)}::SOCKET",
            "Не в сети",
        ]
        qtbot.waitUntil(lambda: window.connection.text() == "● онлайн", timeout=WAIT_MS)
        assert window.jobs.table.rowCount() == 0

        # 3. a new job from the dialog (parameters come from the JSON Schema) ----------------
        def fill_and_start(dialog: NewJobDialog) -> QDialog.DialogCode:
            assert dialog.method.currentText().startswith("NRW-100")
            assert dialog.sample.count() == 2
            dialog.sample.setCurrentIndex(1)
            assert dialog.form.values()["points"] == 100
            dialog._submit()
            qtbot.waitUntil(lambda: dialog.created_job is not None, timeout=WAIT_MS)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(NewJobDialog, "exec", fill_and_start)
        window.new_job()

        # 4. the job card opens by itself and the graphs grow while the job runs -------------
        qtbot.waitUntil(lambda: len(window._cards) == 1, timeout=WAIT_MS)
        card = next(iter(window._cards.values()))
        assert isinstance(window.tabs.currentWidget(), JobCard)
        seen: list[int] = []
        QTimer.singleShot(0, lambda: None)

        def finished() -> bool:
            seen.append(card.curve_points())
            return card.status == "completed" and card.curve_points() == 100

        qtbot.waitUntil(finished, timeout=WAIT_MS)
        assert any(0 < n < 100 for n in seen), f"the graph never showed a partial sweep: {seen}"
        assert card.progress.value() == 100
        assert card.status_label.text() == "Завершено"
        assert "Статус: Измеряется" in card.log_text()

        # 5. the lists were updated by the live events -----------------------------------------
        qtbot.waitUntil(
            lambda: (
                window.jobs.table.rowCount() == 1 and cells(window.jobs.table, 0)[4] == "Завершено"
            ),
            timeout=WAIT_MS,
        )
        assert cells(window.jobs.table, 0)[1:4] == ["VNA-1", "F-001", "NRW-100"]
        qtbot.waitUntil(
            lambda: window.instruments.status_of(seed.instrument_id) == "online", timeout=WAIT_MS
        )

        # 6. the result card: eps and mu --------------------------------------------------------
        assert card.result_button.isEnabled()
        card.result_button.click()
        qtbot.waitUntil(lambda: len(window._results) == 1, timeout=WAIT_MS)
        result = next(iter(window._results.values()))
        assert isinstance(window.tabs.currentWidget(), ResultCard)
        qtbot.waitUntil(lambda: result.result is not None, timeout=WAIT_MS)
        assert "ε′ = 10" in result.summary.text()
        assert "ε″ = 0.5" in result.summary.text()
        assert "μ′ = 1.5" in result.summary.text()
        assert "μ″ = 0.2" in result.summary.text()
        _, eps = result.curves["eps_re"].getData()
        assert eps.mean() == pytest.approx(10, rel=0.02)

        # 7. export ----------------------------------------------------------------------------
        csv_path, json_path = tmp_path / "result.csv", tmp_path / "result.json"
        choices = iter([str(csv_path), str(json_path)])
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (next(choices), ""))
        )
        result.csv_button.click()
        qtbot.waitUntil(csv_path.exists, timeout=WAIT_MS)
        result.json_button.click()
        qtbot.waitUntil(json_path.exists, timeout=WAIT_MS)
        qtbot.waitUntil(lambda: "result.json" in result.status.text(), timeout=WAIT_MS)

        rows = list(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
        assert rows[0] == [
            "freq_hz",
            "eps_re",
            "eps_im",
            "mu_re",
            "mu_im",
            "phase_ambiguity",
            "invalid",
        ]
        assert len(rows) == 101
        exported = json.loads(json_path.read_text(encoding="utf-8"))
        assert exported["job_id"] == card.job_id
        assert exported["summary"]["mu_re_mean"] == pytest.approx(1.5, rel=0.02)
        assert errors == []
    finally:
        window.close()


def seed_port(seed: Seed, live_server: LiveServer) -> int:
    """Port of the simulator, as stored in the instrument's VISA address."""
    import httpx

    with httpx.Client(base_url=f"{live_server.url}/api/v1") as http:
        token = http.post("/auth/login", json={"username": "alice", "password": PASSWORD})
        http.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        resource = http.get(f"/instruments/{seed.instrument_id}").json()["resource"]
    return int(resource.split("::")[2])


def test_expired_session_asks_for_a_new_login_and_keeps_the_open_cards(
    qtbot: QtBot,
    qapp: object,
    live_server: LiveServer,
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
    errors: list[str],
) -> None:
    controller = Controller(ApiClient(live_server.url))
    controller.api.login("alice", PASSWORD)
    window = MainWindow(controller, "alice")
    qtbot.addWidget(window)
    window.show()
    window.start()
    try:
        qtbot.waitUntil(lambda: window.instruments.table.rowCount() == 1, timeout=WAIT_MS)

        shown: list[LoginDialog] = []

        def relogin(dialog: LoginDialog) -> QDialog.DialogCode:
            shown.append(dialog)
            dialog.password.setText(PASSWORD)
            dialog._submit()
            qtbot.waitUntil(lambda: controller.api.token != "expired-or-garbage", timeout=WAIT_MS)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(LoginDialog, "exec", relogin)
        controller.api._token = "expired-or-garbage"
        window.refresh()  # any call now gets a 401

        qtbot.waitUntil(lambda: len(shown) == 1, timeout=WAIT_MS)
        assert shown[0].username.text() == "alice"
        qtbot.waitUntil(lambda: window.instruments.table.rowCount() == 1, timeout=WAIT_MS)
        qtbot.waitUntil(lambda: window.connection.text() == "● онлайн", timeout=WAIT_MS)
        assert errors == []  # an expired session is not reported as an error
    finally:
        window.close()


def test_errors_are_shown_to_the_user(
    qtbot: QtBot, qapp: object, live_server: LiveServer, errors: list[str]
) -> None:
    controller = Controller(ApiClient(live_server.url))
    controller.api.login("alice", PASSWORD)
    window = MainWindow(controller, "alice")
    qtbot.addWidget(window)
    window.show()
    try:
        controller.run(lambda: controller.api.get_job("00000000-0000-0000-0000-000000000000"))
        qtbot.waitUntil(lambda: len(errors) == 1, timeout=WAIT_MS)
        assert "does not exist" in errors[0]
    finally:
        window.close()
