"""Parameter form built from the JSON Schema served by the API (MH-50, FR-06, FR-36)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QWidget,
)

LABELS = {
    "start_hz": "Начальная частота, Гц",
    "stop_hz": "Конечная частота, Гц",
    "points": "Число точек",
    "ifbw_hz": "Полоса ПЧ, Гц",
    "segment_size": "Размер сегмента, точек",
    "averaging": "Усреднение",
    "max_retries": "Повторов сегмента",
}
INT32_MAX = 2**31 - 1


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    label: str
    kind: str  # "int" | "float" | "bool" | "str"
    minimum: float | None = None
    maximum: float | None = None
    default: Any = None


def _kind(prop: dict[str, Any]) -> str:
    kinds = [prop["type"]] if "type" in prop else [p.get("type") for p in prop.get("anyOf", [])]
    for kind in kinds:
        if kind in {"integer", "number", "boolean", "string"}:
            return {"integer": "int", "number": "float", "boolean": "bool", "string": "str"}[kind]
    return "str"


def fields_from_schema(schema: dict[str, Any]) -> list[FieldSpec]:
    """One field per property of the schema, in schema order."""
    fields = []
    for name, prop in schema.get("properties", {}).items():
        minimum = prop.get("minimum", prop.get("exclusiveMinimum"))
        maximum = prop.get("maximum", prop.get("exclusiveMaximum"))
        fields.append(
            FieldSpec(
                name=name,
                label=LABELS.get(name, prop.get("title", name)),
                kind=_kind(prop),
                minimum=minimum,
                maximum=maximum,
                default=prop.get("default"),
            )
        )
    return fields


class SchemaForm(QWidget):
    """Fields for the properties of a JSON Schema; ``values()`` returns the typed values."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QFormLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._editors: dict[str, QWidget] = {}
        self.fields: list[FieldSpec] = []

    def set_schema(self, schema: dict[str, Any]) -> None:
        while self._layout.rowCount():
            self._layout.removeRow(0)
        self._editors.clear()
        self.fields = fields_from_schema(schema)
        for spec in self.fields:
            editor = self._make_editor(spec)
            editor.setObjectName(f"param_{spec.name}")
            self._editors[spec.name] = editor
            self._layout.addRow(spec.label, editor)
        self.set_values({s.name: s.default for s in self.fields if s.default is not None})

    @staticmethod
    def _make_editor(spec: FieldSpec) -> QWidget:
        if spec.kind == "int":
            spin = QSpinBox()
            spin.setRange(
                int(spec.minimum if spec.minimum is not None else 0),
                int(spec.maximum if spec.maximum is not None else INT32_MAX),
            )
            return spin
        if spec.kind == "float":
            dspin = QDoubleSpinBox()
            high = spec.maximum if spec.maximum is not None else 1e18
            dspin.setRange(spec.minimum if spec.minimum is not None else 0.0, high)
            # frequencies (explicit large maximum) are whole hertz, everything else keeps fractions
            dspin.setDecimals(0 if spec.maximum is not None and spec.maximum >= 1e6 else 3)
            return dspin
        if spec.kind == "bool":
            return QCheckBox()
        return QLineEdit()

    def set_values(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            editor = self._editors.get(name)
            if isinstance(editor, QSpinBox):
                editor.setValue(int(value))
            elif isinstance(editor, QDoubleSpinBox):
                editor.setValue(float(value))
            elif isinstance(editor, QCheckBox):
                editor.setChecked(bool(value))
            elif isinstance(editor, QLineEdit):
                editor.setText(str(value))

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, editor in self._editors.items():
            if isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                result[name] = editor.value()
            elif isinstance(editor, QCheckBox):
                result[name] = editor.isChecked()
            elif isinstance(editor, QLineEdit):
                result[name] = editor.text()
        return result

    def editor(self, name: str) -> QWidget:
        return self._editors[name]
