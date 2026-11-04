"""Widgets of the client against a real server (HTTP + WebSocket), headless."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtWidgets import QDialog, QFileDialog, QTableWidget
from pytestqt.qtbot import QtBot

from measurehub_client.api import ApiClient
from measurehub_client.controller import Controller
from measurehub_client.events import CLOSED, CONNECTED, RECONNECTING, EventStream
from measurehub_client.instruments_tab import InstrumentsTab
from measurehub_client.job_card import JobCard
from measurehub_client.jobs_tab import JobsTab
from measurehub_client.login import LoginDialog
from measurehub_client.new_job import NewJobDialog
from measurehub_client.result_card import ResultCard
from tests.client.conftest import LiveServer, Seed
from tests.conftest import PASSWORD

WAIT_MS = 20_000


def cell(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


def exported_job_id(path: Path) -> str | None:
    """``job_id`` of a JSON export, or None while the file still holds something else."""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("job_id"))
    except ValueError:
        return None


def create_job(ctl: Controller, seed: Seed, params: dict[str, Any] | None = None) -> str:
    job = ctl.api.create_job(seed.method_id, seed.instrument_id, seed.sample_id, params)
    return str(job["id"])


# -- login ---------------------------------------------------------------------------------------


def test_login_with_the_right_password_stores_the_token_and_forgets_the_password(
    qtbot: QtBot, controller: Controller
) -> None:
    dialog = LoginDialog(controller)
    qtbot.addWidget(dialog)
    dialog.username.setText("alice")
    dialog.password.setText(PASSWORD)
    with qtbot.waitSignal(dialog.accepted, timeout=WAIT_MS):
        dialog._submit()
    assert controller.api.logged_in
    assert dialog.password.text() == ""


def test_login_with_a_wrong_password_shows_the_server_message(
    qtbot: QtBot, controller: Controller
) -> None:
    dialog = LoginDialog(controller, "alice")
    qtbot.addWidget(dialog)
    dialog.password.setText("wrong")
    dialog._submit()
    qtbot.waitUntil(lambda: "Wrong username or password" in dialog.status.text(), timeout=WAIT_MS)
    assert not controller.api.logged_in
    assert dialog.buttons.isEnabled()  # can try again
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_login_requires_both_fields_and_reports_an_unreachable_server(
    qtbot: QtBot, qapp: object
) -> None:
    ctl = Controller(ApiClient("http://127.0.0.1:1", timeout=2))
    dialog = LoginDialog(ctl)
    qtbot.addWidget(dialog)
    dialog._submit()
    assert "Введите логин и пароль" in dialog.status.text()
    dialog.username.setText("alice")
    dialog.password.setText("x")
    dialog._submit()
    qtbot.waitUntil(lambda: "Нет связи с сервером" in dialog.status.text(), timeout=WAIT_MS)
    ctl.shutdown()


# -- instruments ---------------------------------------------------------------------------------


def test_instruments_tab_shows_statuses_and_follows_live_events(
    qtbot: QtBot, logged_in: Controller, seed: Seed
) -> None:
    tab = InstrumentsTab(logged_in)
    qtbot.addWidget(tab)
    tab.reload()
    qtbot.waitUntil(lambda: tab.table.rowCount() == 1, timeout=WAIT_MS)
    assert cell(tab.table, 0, 0) == "VNA-1"
    assert tab.status_of(seed.instrument_id) == "offline"

    tab.apply_event({"type": "instrument", "instrument_id": seed.instrument_id, "status": "busy"})
    assert tab.status_of(seed.instrument_id) == "busy"
    assert cell(tab.table, 0, 2) == "Занят"
    tab.apply_event(
        {
            "type": "snapshot",
            "instruments": [{"instrument_id": seed.instrument_id, "status": "online"}],
        }
    )
    assert tab.status_of(seed.instrument_id) == "online"
    tab.apply_event({"type": "instrument", "instrument_id": "unknown", "status": "error"})


def test_check_button_asks_the_instrument(qtbot: QtBot, logged_in: Controller, seed: Seed) -> None:
    tab = InstrumentsTab(logged_in)
    qtbot.addWidget(tab)
    tab.reload()
    qtbot.waitUntil(lambda: tab.table.rowCount() == 1, timeout=WAIT_MS)
    tab.table.selectRow(0)
    tab.check_selected()
    qtbot.waitUntil(lambda: tab.status_of(seed.instrument_id) == "online", timeout=WAIT_MS)
    assert cell(tab.table, 0, 3) == "MeasureHub,SIM-VNA-01,000001,1.0"


# -- new job and job list ------------------------------------------------------------------------


def new_job_dialog(qtbot: QtBot, ctl: Controller) -> NewJobDialog:
    api = ctl.api
    dialog = NewJobDialog(
        ctl,
        api.list_methods(),
        api.list_instruments(),
        api.list_samples(),
        api.measurement_schema(),
    )
    qtbot.addWidget(dialog)
    return dialog


def test_new_job_dialog_prefills_the_method_and_sends_only_the_changes(
    qtbot: QtBot, logged_in: Controller, seed: Seed
) -> None:
    dialog = new_job_dialog(qtbot, logged_in)
    assert dialog.form.values()["points"] == 100  # the method's own value
    assert dialog.overrides() == {}

    dialog.form.set_values({"points": 50, "segment_size": 25})
    assert dialog.overrides() == {"points": 50}
    dialog.sample.setCurrentIndex(1)
    with qtbot.waitSignal(dialog.accepted, timeout=WAIT_MS):
        dialog._submit()
    job = dialog.created_job
    assert job is not None
    assert job["points_total"] == 50
    assert job["sample_id"] == seed.sample_id


def test_new_job_dialog_shows_the_validation_error_of_the_server(
    qtbot: QtBot, logged_in: Controller, seed: Seed
) -> None:
    dialog = new_job_dialog(qtbot, logged_in)
    dialog.form.set_values({"start_hz": 9e9, "stop_hz": 2e9})
    dialog.sample.setCurrentIndex(1)
    dialog._submit()
    qtbot.waitUntil(lambda: "stop_hz must be greater" in dialog.status.text(), timeout=WAIT_MS)
    assert dialog.created_job is None
    assert dialog.buttons.isEnabled()


def test_nrw_without_a_sample_is_refused_with_the_server_reason(
    qtbot: QtBot, logged_in: Controller, seed: Seed
) -> None:
    dialog = new_job_dialog(qtbot, logged_in)
    dialog._submit()
    qtbot.waitUntil(lambda: "requires a sample" in dialog.status.text(), timeout=WAIT_MS)


def test_job_list_filters_by_status_and_opens_a_job(
    qtbot: QtBot, logged_in: Controller, seed: Seed, live_server: LiveServer
) -> None:
    job_id = create_job(logged_in, seed)
    live_server.wait_for_jobs()
    api = logged_in.api
    tab = JobsTab(logged_in)
    qtbot.addWidget(tab)
    tab.set_names(api.list_instruments(), api.list_samples(), api.list_methods())
    tab.reload()
    qtbot.waitUntil(lambda: tab.table.rowCount() == 1, timeout=WAIT_MS)
    assert [cell(tab.table, 0, c) for c in (1, 2, 3, 4)] == [
        "VNA-1",
        "F-001",
        "NRW-100",
        "Завершено",
    ]
    assert cell(tab.table, 0, 5) == "100% (100/100)"
    assert tab.job_ids() == [job_id]
    assert "Всего заданий: 1" in tab.total.text()

    tab.filter.setCurrentIndex(tab.filter.findData("failed"))
    qtbot.waitUntil(lambda: tab.table.rowCount() == 0, timeout=WAIT_MS)
    tab.filter.setCurrentIndex(0)
    qtbot.waitUntil(lambda: tab.table.rowCount() == 1, timeout=WAIT_MS)

    with qtbot.waitSignal(tab.job_activated, timeout=WAIT_MS) as opened:
        tab.table.cellDoubleClicked.emit(0, 0)
    assert opened.args == [job_id]


def test_status_event_for_an_unknown_job_reloads_the_list(
    qtbot: QtBot, logged_in: Controller, seed: Seed, live_server: LiveServer
) -> None:
    tab = JobsTab(logged_in)
    qtbot.addWidget(tab)
    tab.reload()
    qtbot.waitUntil(lambda: tab.total.text() == "Всего заданий: 0", timeout=WAIT_MS)
    job_id = create_job(logged_in, seed)
    tab.apply_event({"type": "status", "job_id": job_id, "status": "queued"})
    qtbot.waitUntil(lambda: tab.table.rowCount() == 1, timeout=WAIT_MS)
    tab.apply_event({"type": "progress", "job_id": job_id})  # ignored
    live_server.wait_for_jobs()
    tab.apply_event({"type": "status", "job_id": job_id, "status": "completed"})
    assert cell(tab.table, 0, 4) == "Завершено"
    assert cell(tab.table, 0, 5).startswith("100%")


# -- event stream --------------------------------------------------------------------------------


def test_event_stream_gets_a_snapshot_and_reconnects_after_the_server_restarts(
    qtbot: QtBot, logged_in: Controller, live_server: LiveServer, seed: Seed
) -> None:
    stream = EventStream(lambda: logged_in.api.ws_url("/ws/events"), backoff=(0.1, 0.1))
    states: list[str] = []
    messages: list[dict[str, Any]] = []
    stream.state_changed.connect(states.append)
    stream.message.connect(messages.append)
    try:
        with qtbot.waitSignal(stream.message, timeout=WAIT_MS):
            stream.start()
        assert messages[0]["type"] == "snapshot"
        assert [i["status"] for i in messages[0]["instruments"]] == ["offline"]
        assert states == ["connecting", CONNECTED]

        live_server.stop()
        qtbot.waitUntil(lambda: stream.state == RECONNECTING, timeout=WAIT_MS)
        with qtbot.waitSignal(stream.reconnected, timeout=WAIT_MS):
            live_server.start()
        qtbot.waitUntil(lambda: len(messages) >= 2, timeout=WAIT_MS)
        assert messages[1]["type"] == "snapshot"  # the server always starts with a snapshot
        assert stream.state == CONNECTED
    finally:
        stream.stop()
    assert stream.state == CLOSED


def test_event_stream_with_a_bad_token_reports_unauthorized_and_stops(
    qtbot: QtBot, logged_in: Controller, live_server: LiveServer
) -> None:
    url = f"ws://127.0.0.1:{live_server.port}/ws/events?token=garbage"
    stream = EventStream(lambda: url, backoff=(0.1,))
    try:
        with qtbot.waitSignal(stream.unauthorized, timeout=WAIT_MS):
            stream.start()
        assert stream.state == CLOSED
    finally:
        stream.stop()


# -- job card ------------------------------------------------------------------------------------


def open_card(qtbot: QtBot, ctl: Controller, job_id: str) -> JobCard:
    card = JobCard(ctl, job_id)
    qtbot.addWidget(card)
    card.load()
    return card


def test_job_card_fills_the_graphs_live_and_ends_completed(
    qtbot: QtBot, logged_in: Controller, seed: Seed
) -> None:
    job_id = create_job(logged_in, seed)
    card = open_card(qtbot, logged_in, job_id)
    seen: list[int] = []

    def sample() -> bool:
        seen.append(card.curve_points())
        return card.status == "completed" and card.curve_points() == 100

    qtbot.waitUntil(sample, timeout=WAIT_MS)
    assert any(0 < n < 100 for n in seen), seen  # the graph grew while the job ran
    assert card.progress.value() == 100
    assert card.progress.format() == "100 / 100 точек"
    assert card.result_button.isEnabled()
    assert not card.cancel_button.isEnabled()
    assert not card.retry_button.isEnabled()
    text = card.log_text()
    assert "Статус: Измеряется" in text
    assert "Статус: Завершено" in text
    assert card.error_label.text() == ""
    card.stop_stream()


def test_card_opened_in_the_middle_shows_what_was_measured_and_follows_the_rest(
    qtbot: QtBot, logged_in: Controller, seed: Seed
) -> None:
    job_id = create_job(logged_in, seed)
    qtbot.waitUntil(lambda: logged_in.api.get_job(job_id)["points_done"] >= 50, timeout=WAIT_MS)

    card = open_card(qtbot, logged_in, job_id)

    qtbot.waitUntil(lambda: card.curve_points() >= 50, timeout=WAIT_MS)
    qtbot.waitUntil(
        lambda: card.status == "completed" and card.curve_points() == 100, timeout=WAIT_MS
    )
    card.stop_stream()


def test_cancel_stops_the_job_and_retry_opens_a_new_one(
    qtbot: QtBot, logged_in: Controller, seed: Seed
) -> None:
    job_id = create_job(logged_in, seed, {"points": 200, "segment_size": 20})
    card = open_card(qtbot, logged_in, job_id)
    qtbot.waitUntil(
        lambda: card.curve_points() >= 20 and card.cancel_button.isEnabled(), timeout=WAIT_MS
    )

    card.cancel_button.click()

    qtbot.waitUntil(lambda: card.status == "cancelled", timeout=WAIT_MS)
    assert 20 <= card.curve_points() < 200
    assert card.retry_button.isEnabled()
    assert not card.cancel_button.isEnabled()
    assert not card.result_button.isEnabled()

    with qtbot.waitSignal(logged_in.open_job_requested, timeout=WAIT_MS) as requested:
        card.retry_button.click()
    assert requested.args[0] != job_id
    new_job = logged_in.api.get_job(requested.args[0])
    assert new_job["params"] == card.job["params"]  # type: ignore[index]
    logged_in.api.cancel_job(new_job["id"])
    card.stop_stream()


def test_failed_job_shows_the_reason(
    qtbot: QtBot, logged_in: Controller, seed: Seed, slow_sim: Any
) -> None:
    # point the instrument at a port where nothing listens
    http = logged_in.api._http
    http.patch(
        f"/instruments/{seed.instrument_id}",
        json={"resource": "TCPIP0::127.0.0.1::1::SOCKET"},
        headers={"Authorization": f"Bearer {logged_in.api.token}"},
    )
    job_id = create_job(logged_in, seed, {"max_retries": 0})
    card = open_card(qtbot, logged_in, job_id)
    qtbot.waitUntil(lambda: card.status == "failed", timeout=WAIT_MS)
    qtbot.waitUntil(lambda: "нет связи с прибором" in card.error_label.text(), timeout=WAIT_MS)
    assert card.retry_button.isEnabled()
    assert "Ошибка:" in card.log_text()
    card.stop_stream()


def test_card_repairs_its_data_after_the_connection_dropped(
    qtbot: QtBot, logged_in: Controller, seed: Seed, live_server: LiveServer
) -> None:
    job_id = create_job(logged_in, seed, {"points": 200, "segment_size": 20})
    card = open_card(qtbot, logged_in, job_id)
    qtbot.waitUntil(lambda: card.curve_points() >= 20, timeout=WAIT_MS)

    live_server.stop()  # the worker keeps measuring, the client misses the events
    qtbot.waitUntil(lambda: "переподключение" in card.connection_label.text(), timeout=WAIT_MS)
    live_server.wait_for_jobs()
    live_server.start()

    qtbot.waitUntil(
        lambda: card.status == "completed" and card.curve_points() == 200, timeout=WAIT_MS
    )
    assert "Связь восстановлена" in card.log_text()
    card.stop_stream()


# -- result card ---------------------------------------------------------------------------------


def test_result_card_draws_eps_and_mu_and_exports(
    qtbot: QtBot,
    logged_in: Controller,
    seed: Seed,
    live_server: LiveServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = create_job(logged_in, seed)
    live_server.wait_for_jobs()
    card = ResultCard(logged_in, job_id)
    qtbot.addWidget(card)
    card.load()
    qtbot.waitUntil(lambda: card.result is not None, timeout=WAIT_MS)

    assert "ε′ = 10" in card.summary.text()
    assert "μ′ = 1.5" in card.summary.text()
    assert card.quality.text() == "Качество: все точки в порядке"
    for name, curve in card.curves.items():
        x, y = curve.getData()
        assert len(x) == len(y) == 100, name
    x, y = card.curves["eps_re"].getData()
    assert x[0] == pytest.approx(1.0)  # GHz
    assert y.mean() == pytest.approx(10, rel=0.02)

    target = tmp_path / "out"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    card.csv_button.click()
    qtbot.waitUntil(lambda: target.exists() and "Сохранено" in card.status.text(), timeout=WAIT_MS)
    rows = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8"))))
    assert rows[0][:5] == ["freq_hz", "eps_re", "eps_im", "mu_re", "mu_im"]
    assert len(rows) == 101

    card.json_button.click()
    qtbot.waitUntil(lambda: exported_job_id(target) == job_id, timeout=WAIT_MS)
    assert json.loads(target.read_text(encoding="utf-8"))["summary"][
        "eps_re_mean"
    ] == pytest.approx(10, rel=0.02)

    card.raw_button.click()
    qtbot.waitUntil(
        lambda: target.read_text(encoding="utf-8").startswith("freq_hz,s11_re"), timeout=WAIT_MS
    )

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    before = target.read_bytes()
    card.csv_button.click()  # cancelled in the dialog: nothing is written
    qtbot.wait(200)
    assert target.read_bytes() == before
