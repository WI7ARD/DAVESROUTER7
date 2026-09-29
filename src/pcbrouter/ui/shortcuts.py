"""Single-letter shortcut guard: don't fire actions while the user is typing."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
)


def typing_focus() -> bool:
    """Whether keyboard focus sits in a text-entry widget.

    Window-wide single-letter shortcuts (R route, T draw, L lock, F fit, G
    grid) must yield while the user types in a filter box, prompt field, or
    numeric editor — otherwise typing a net name routes the board.
    """
    w = QApplication.focusWidget()
    if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return True
    return isinstance(w, QComboBox) and w.isEditable()
