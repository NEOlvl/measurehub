"""CSV rendering of results and raw points."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from typing import Any

RAW_COLUMNS = ("freq_hz", "s11_re", "s11_im", "s21_re", "s21_im")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.9g}"
    return str(value)


def result_csv(data: dict[str, Any]) -> str:
    """One row per frequency: ``freq_hz``, result columns, then quality flags (0/1)."""
    columns: dict[str, list[Any]] = data["columns"]
    flags: dict[str, list[Any]] = data["flags"]
    series = {"freq_hz": data["freq_hz"]}
    series.update({name: columns[name] for name in data.get("column_order", columns)})
    series.update({name: flags[name] for name in data.get("flag_order", flags)})
    return _write(list(series), zip(*series.values(), strict=True))


def raw_csv(rows: Sequence[Sequence[float]]) -> str:
    return _write(list(RAW_COLUMNS), rows)


def _write(header: list[str], rows: Any) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    return out.getvalue()
