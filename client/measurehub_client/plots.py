"""pyqtgraph helpers shared by the job and result cards."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from numpy.typing import NDArray

pg.setConfigOptions(background="w", foreground="k", antialias=True)

PALETTE = ("#2980b9", "#c0392b", "#27ae60", "#8e44ad")


def make_plot(title: str, y_label: str, x_label: str = "Частота, ГГц") -> pg.PlotWidget:
    plot = pg.PlotWidget(title=title)
    plot.setLabel("left", y_label)
    plot.setLabel("bottom", x_label)
    plot.showGrid(x=True, y=True, alpha=0.3)
    plot.addLegend(offset=(-10, 10))
    return plot


def add_curve(plot: pg.PlotWidget, name: str, color_index: int = 0) -> pg.PlotDataItem:
    pen = pg.mkPen(PALETTE[color_index % len(PALETTE)], width=2)
    return plot.plot([], [], pen=pen, name=name, connect="finite")


def set_curve(curve: pg.PlotDataItem, x: NDArray[np.float64], y: NDArray[np.float64]) -> None:
    """Update a curve; ``nan`` values (not measured yet) leave gaps instead of lines."""
    curve.setData(x, y, connect="finite")


def curve_data(curve: pg.PlotDataItem) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """The data currently shown by a curve (for tests and tooltips)."""
    x, y = curve.getData()
    if x is None or y is None:
        return np.empty(0), np.empty(0)
    return np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
