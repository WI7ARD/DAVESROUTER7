"""Dialogs for the AI layer: privacy disclosure, context preview, constraint editor,
usage. All text originating from the board or a model is displayed as plain text or
HTML-escaped; nothing from a model is ever rendered as live HTML."""

from __future__ import annotations

import html
import typing
from typing import Any

from pydantic import ValidationError
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pcbrouter.ai.command_schema import DifferentialPairSpec, EntityRef, RoutingConstraints
from pcbrouter.ai.context_builder import BoardContext
from pcbrouter.ai.usage import UsageTracker, format_token_estimate

NOT_SPECIFIED = "Not specified"


def _mono() -> QFont:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


class ContextPreviewDialog(QDialog):
    """Shows exactly what board context would be sent."""

    def __init__(
        self,
        context: BoardContext,
        provider_name: str,
        parent: QWidget | None = None,
        full_message: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Board context being shared with provider")
        self.resize(820, 620)
        facts = QLabel(
            f"Provider: <b>{_esc(provider_name)}</b><br>"
            f"Context level: {context.level.label} · {context.char_count:,} characters · "
            f"{format_token_estimate(context.token_estimate)} (approximate)<br>"
            f"Nets listed: {context.included_nets}/{context.total_nets} · Components listed: "
            f"{context.included_components}/{context.total_components}"
            + (" · <b>truncated to limits</b>" if context.truncated else "")
            + "<br>Anonymised: "
            + (_esc(", ".join(context.anonymized)) or "nothing")
            + f"<br>Board fingerprint: <code>{context.board_fingerprint[:16]}…</code>"
            + "<br><b>The original KiCad file is not uploaded.</b>"
        )
        facts.setWordWrap(True)
        text = QPlainTextEdit(full_message or context.text)
        text.setReadOnly(True)
        text.setFont(_mono())
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text = text
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(facts)
        layout.addWidget(text, 1)
        layout.addWidget(buttons)


class PrivacyDisclosureDialog(QDialog):
    """Shown before the first request for a board (unless turned off)."""

    def __init__(
        self, context: BoardContext, provider_name: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Share board context with an AI provider?")
        self._context = context
        self._provider = provider_name
        items = "".join(f"<li>{_esc(d)}</li>" for d in context.disclosure)
        body = QLabel(
            f"This request will send the following engineering context to "
            f"<b>{_esc(provider_name)}</b>:<ul>{items}</ul>"
            "The original KiCad PCB file will <b>NOT</b> be uploaded. Your prompt is sent as "
            "typed" + (" (with anonymised names)" if context.anonymized else "") + ".<br>"
            f"Approximate size: {format_token_estimate(context.token_estimate)}."
        )
        body.setWordWrap(True)
        self.dont_show = QCheckBox("Don't show this again (you can re-enable it in Settings)")
        view = QPushButton("View Context…")
        view.clicked.connect(self._view)
        buttons = QDialogButtonBox()
        buttons.addButton("Continue", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(view, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(body)
        layout.addWidget(self.dont_show)
        layout.addWidget(buttons)

    def _view(self) -> None:
        ContextPreviewDialog(self._context, self._provider, self).exec()


# ======================================================================= constraints
def _optional_inner(annotation: Any) -> Any:
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    return args[0] if len(args) == 1 else annotation


def _literal_values(annotation: Any) -> tuple[str, ...] | None:
    inner = _optional_inner(annotation)
    if typing.get_origin(inner) is typing.Literal:
        return tuple(str(v) for v in typing.get_args(inner))
    return None


_BOOL = {"minimize_vias", "preserve_existing_routes", "allow_ripup", "allow_component_movement"}
_INT = {"max_vias"}
_LISTS = {"preferred_layers", "forbidden_layers", "avoid_nets", "avoid_net_classes"}
_ENTITIES = {"keep_near", "keep_away_from"}
_PAIR = "differential_pair"
_TEXT = {"additional_notes"}

EDITABLE_FIELDS: tuple[str, ...] = tuple(RoutingConstraints.model_fields)


def _label(name: str) -> str:
    text = name.replace("_mm", " (mm)").replace("_ohm", " (Ω)").replace("_", " ")
    return text[0].upper() + text[1:]


class ConstraintEditorDialog(QDialog):
    """Typed editor for :class:`RoutingConstraints`. Every field is covered."""

    def __init__(
        self,
        constraints: RoutingConstraints,
        parent: QWidget | None = None,
        title: str = "Edit Constraints",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 700)
        self._widgets: dict[str, QWidget] = {}
        values = constraints.model_dump()
        form_host = QWidget()
        form = QFormLayout(form_host)
        for name, info in RoutingConstraints.model_fields.items():
            widget = self._make_widget(name, info.annotation, values.get(name))
            self._widgets[name] = widget
            form.addRow(_label(name) + ":", widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_host)
        hint = QLabel(
            "Blank / 'Not specified' leaves a constraint unset (safe defaults apply: "
            "preserve routes, no rip-up, no component movement). Lists are comma "
            "separated; entity lists use net:NAME or component:REF."
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #e5534b;")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self.result_constraints: RoutingConstraints | None = None

    def _make_widget(self, name: str, annotation: Any, value: Any) -> QWidget:
        choices = _literal_values(annotation)
        if name in _BOOL or choices is not None:
            combo = QComboBox()
            combo.addItem(NOT_SPECIFIED, None)
            if name in _BOOL:
                combo.addItem("Yes", True)
                combo.addItem("No", False)
            else:
                for c in choices or ():
                    combo.addItem(c.replace("_", " "), c)
            idx = combo.findData(value)
            combo.setCurrentIndex(max(idx, 0))
            return combo
        if name in _INT:
            spin = QSpinBox()
            spin.setRange(-1, 256)
            spin.setSpecialValueText(NOT_SPECIFIED)
            spin.setValue(-1 if value is None else int(value))
            return spin
        if name in _LISTS or name in _TEXT:
            edit = QLineEdit(", ".join(value) if isinstance(value, list) else (value or ""))
            return edit
        if name in _ENTITIES:
            text = ", ".join(f"{e['kind']}:{e['name']}" for e in value or [])
            return QLineEdit(text)
        if name == _PAIR:
            return QLineEdit(f"{value['positive_net']}, {value['negative_net']}" if value else "")
        spin_f = QDoubleSpinBox()  # remaining fields are optional floats
        spin_f.setDecimals(3)
        spin_f.setRange(-1.0, 10_000.0)
        spin_f.setSingleStep(0.05)
        spin_f.setSpecialValueText(NOT_SPECIFIED)
        spin_f.setValue(-1.0 if value is None else float(value))
        return spin_f

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, w in self._widgets.items():
            value: Any = None
            if isinstance(w, QComboBox):
                value = w.currentData()
            elif isinstance(w, QSpinBox):
                value = None if w.value() < 0 else w.value()
            elif isinstance(w, QDoubleSpinBox):
                value = None if w.value() < 0 else round(w.value(), 6)
            elif isinstance(w, QLineEdit):
                text = w.text().strip()
                if not text:
                    value = None
                elif name in _LISTS:
                    value = [p.strip() for p in text.split(",") if p.strip()]
                elif name in _ENTITIES:
                    value = []
                    for part in (p.strip() for p in text.split(",") if p.strip()):
                        kind, _, ident = part.partition(":")
                        value.append(
                            EntityRef.model_validate(
                                {"kind": kind.strip(), "name": ident.strip()}
                            ).model_dump()
                        )
                elif name == _PAIR:
                    parts = [p.strip() for p in text.split(",")]
                    if len(parts) != 2:
                        raise ValueError("differential pair: enter 'POSITIVE, NEGATIVE'")
                    value = DifferentialPairSpec(
                        positive_net=parts[0], negative_net=parts[1]
                    ).model_dump()
                else:
                    value = text
            if value is not None:
                out[name] = value
        return out

    def _accept(self) -> None:
        try:
            self.result_constraints = RoutingConstraints.model_validate(self.values())
        except (ValidationError, ValueError) as exc:
            self.error.setText(f"Not saved: {exc}")
            return
        self.accept()


class UsageDialog(QDialog):
    def __init__(self, tracker: UsageTracker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Usage (this session)")
        self.resize(760, 420)
        s = tracker.summary()
        summary = QLabel(
            f"Requests: <b>{s.requests}</b> ({s.failed_requests} failed/cancelled)<br>"
            f"Input tokens: <b>{s.input_tokens:,}</b> · Output tokens: <b>{s.output_tokens:,}</b>"
            + (
                f" · {s.requests_without_usage} request(s) reported no usage"
                if s.requests_without_usage
                else ""
            )
            + f"<br>Estimated cost: {_esc(s.estimated_cost)}"
        )
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["Provider", "Model", "Input", "Output", "Latency", "OK"])
        for r in tracker.records:
            row = table.rowCount()
            table.insertRow(row)
            for col, val in enumerate(
                (
                    r.profile_name,
                    r.model,
                    "unknown" if r.input_tokens is None else f"{r.input_tokens:,}",
                    "unknown" if r.output_tokens is None else f"{r.output_tokens:,}",
                    "—" if r.latency_s is None else f"{r.latency_s:.1f} s",
                    "yes" if r.succeeded else "no",
                )
            ):
                table.setItem(row, col, QTableWidgetItem(val))
        table.resizeColumnsToContents()
        self.table = table
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(table, 1)
        layout.addWidget(buttons)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)
