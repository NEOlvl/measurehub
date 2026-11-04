"""Sweep buffer, JSON-Schema form and small helpers."""

from __future__ import annotations

import math

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from measurehub.drivers.models import VnaSweepParams
from measurehub_client import format as fmt
from measurehub_client.buffer import SweepBuffer, to_db
from measurehub_client.events import reconnect_delay
from measurehub_client.schema_form import SchemaForm, fields_from_schema

PARAMS = {"start_hz": 1e9, "stop_hz": 2e9, "points": 5}


def test_buffer_axis_comes_from_the_job_parameters() -> None:
    buffer = SweepBuffer.for_params(PARAMS)
    assert buffer.freq_hz.tolist() == [1e9, 1.25e9, 1.5e9, 1.75e9, 2e9]
    assert buffer.filled == 0
    assert np.isnan(buffer.s21_db).all()


def test_segments_fill_the_buffer_and_leave_gaps_as_nan() -> None:
    buffer = SweepBuffer.for_params(PARAMS)
    buffer.add_segment(3, [-1.0, -2.0], [-10.0, -20.0])
    assert buffer.filled == 2
    assert np.isnan(buffer.s11_db[:3]).all()
    assert buffer.s11_db[3:].tolist() == [-1.0, -2.0]
    assert buffer.s21_db[3:].tolist() == [-10.0, -20.0]


def test_points_from_the_api_and_segments_describe_the_same_data() -> None:
    points = [
        {"idx": 0, "s11_re": 0.1, "s11_im": 0.0, "s21_re": 0.0, "s21_im": 0.5},
        {"idx": 1, "s11_re": 0.0, "s11_im": 0.1, "s21_re": 0.5, "s21_im": 0.0},
    ]
    a = SweepBuffer.for_params(PARAMS)
    a.add_points(points)
    b = SweepBuffer.for_params(PARAMS)
    b.add_segment(0, [-20.0, -20.0], [20 * math.log10(0.5)] * 2)
    assert np.allclose(a.s11_db[:2], b.s11_db[:2])
    assert np.allclose(a.s21_db[:2], b.s21_db[:2])
    a.add_points(points)  # the same points again (after a reconnect) change nothing
    assert a.filled == 2
    a.add_points([])


def test_zero_magnitude_does_not_give_minus_infinity() -> None:
    assert np.isfinite(to_db(np.array([0.0]))).all()


def test_reconnect_backoff_grows_and_then_stays() -> None:
    delays = [reconnect_delay(n) for n in range(8)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0]


def test_texts() -> None:
    assert fmt.job_status_text("running") == "Измеряется"
    assert fmt.job_status_text("weird") == "weird"
    assert fmt.progress_percent(25, 100) == 25
    assert fmt.progress_percent(0, 0) == 0
    assert fmt.local_time(None) == "—"
    assert len(fmt.local_time("2026-11-05T12:00:00Z")) == 19
    assert fmt.error_summary(None, "x") == ""
    assert fmt.error_summary("instrument_timeout", "TMO") == "прибор не отвечает: TMO"


# -- schema form -----------------------------------------------------------------------------------


def test_fields_follow_the_json_schema_of_the_sweep() -> None:
    schema = VnaSweepParams.model_json_schema()
    fields = {f.name: f for f in fields_from_schema(schema)}
    assert list(fields) == [
        "start_hz",
        "stop_hz",
        "points",
        "ifbw_hz",
        "segment_size",
        "averaging",
        "max_retries",
    ]
    assert fields["points"].kind == "int"
    assert (fields["points"].minimum, fields["points"].maximum) == (2, 10_001)
    assert fields["start_hz"].kind == "float"
    assert fields["start_hz"].default == 1e9
    assert fields["start_hz"].label == "Начальная частота, Гц"


def test_unknown_field_gets_the_schema_title_and_type() -> None:
    schema = {
        "properties": {
            "note": {"type": "string", "title": "Note"},
            "fast": {"type": "boolean", "default": True},
            "gain": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        }
    }
    fields = {f.name: f for f in fields_from_schema(schema)}
    assert (fields["note"].kind, fields["note"].label) == ("str", "Note")
    assert fields["fast"].kind == "bool"
    assert fields["gain"].kind == "float"


def test_form_round_trips_typed_values(qtbot: QtBot) -> None:
    form = SchemaForm()
    qtbot.addWidget(form)
    form.set_schema(VnaSweepParams.model_json_schema())
    assert form.values() == VnaSweepParams().model_dump()  # defaults come from the schema

    form.set_values({"points": 501, "stop_hz": 6e9, "averaging": 4})
    values = form.values()
    assert (values["points"], values["stop_hz"], values["averaging"]) == (501, 6e9, 4)
    assert isinstance(values["points"], int)
    assert isinstance(values["stop_hz"], float)


def test_form_limits_come_from_the_schema(qtbot: QtBot) -> None:
    form = SchemaForm()
    qtbot.addWidget(form)
    form.set_schema(VnaSweepParams.model_json_schema())
    form.set_values({"points": 10})
    form.editor("points").setValue(10_000_000)  # type: ignore[attr-defined]
    assert form.values()["points"] == 10_001
    form.editor("points").setValue(0)  # type: ignore[attr-defined]
    assert form.values()["points"] == 2


def test_form_is_rebuilt_for_a_new_schema(qtbot: QtBot) -> None:
    form = SchemaForm()
    qtbot.addWidget(form)
    form.set_schema(VnaSweepParams.model_json_schema())
    form.set_schema({"properties": {"gain": {"type": "number", "default": 2.5}}})
    assert list(form.values()) == ["gain"]
    assert form.values()["gain"] == pytest.approx(2.5)
