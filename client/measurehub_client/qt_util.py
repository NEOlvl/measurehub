"""Small helpers that satisfy the Optional return types of the Qt stubs."""

from __future__ import annotations

from PyQt6.QtWidgets import QDialogButtonBox, QMainWindow, QStatusBar, QTableWidget


def set_button_text(
    box: QDialogButtonBox, which: QDialogButtonBox.StandardButton, text: str
) -> None:
    button = box.button(which)
    assert button is not None
    button.setText(text)


def stretch_last_column(table: QTableWidget) -> None:
    header = table.horizontalHeader()
    assert header is not None
    header.setStretchLastSection(True)


def status_bar(window: QMainWindow) -> QStatusBar:
    bar = window.statusBar()
    assert bar is not None
    return bar
