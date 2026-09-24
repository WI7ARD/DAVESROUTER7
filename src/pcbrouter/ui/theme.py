"""Visual theme: application palette, stylesheet and PCB layer colours.

The PCB canvas always uses a dark workspace (best contrast for copper); the
``theme`` setting controls the surrounding widgets.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from pcbrouter.domain.layer import BACK_COPPER, EDGE_CUTS, FRONT_COPPER, inner_layer_index
from pcbrouter.settings.settings import Theme

# ------------------------------------------------------------------ canvas colours
CANVAS_BACKGROUND = QColor("#0e1116")
BOARD_SUBSTRATE = QColor("#171c22")
GRID_MINOR = QColor("#1c222a")
GRID_MAJOR = QColor("#28303a")
OUTLINE_COLOR = QColor("#e3c745")
FOOTPRINT_OUTLINE = QColor("#8a94a3")
LABEL_COLOR = QColor("#d7dde5")
THT_PAD_COLOR = QColor("#c9a227")
NPTH_COLOR = QColor("#5b6470")
VIA_COLOR = QColor("#b9c0ca")
BLIND_VIA_COLOR = QColor("#5fb3b3")
MICRO_VIA_COLOR = QColor("#b083e0")
SELECTION_COLOR = QColor("#ffffff")
HOVER_COLOR = QColor("#7dd3fc")
NET_HIGHLIGHT_COLOR = QColor("#fde047")

# Stage 3 engineering overlays (display only; never board data).
DRC_ERROR_COLOR = QColor("#ff3b30")
DRC_WARNING_COLOR = QColor("#ffb020")
DRC_INFO_COLOR = QColor("#7dd3fc")
CANDIDATE_VALID_COLOR = QColor("#34d399")
CANDIDATE_INVALID_COLOR = QColor("#ff3b30")
CANDIDATE_UNKNOWN_COLOR = QColor("#ffb020")
ENVELOPE_COLOR = QColor("#f472b6")
KEEPOUT_COLOR = QColor("#ef4444")
BOUNDARY_COLOR = QColor("#22d3ee")
RAW_BOUNDS_COLOR = QColor("#a3a3a3")
INFLATED_COLOR = QColor("#f59e0b")
AIRWIRE_COLOR = QColor("#e5e7eb")
#: Occupancy-grid cell colours (RGBA) indexed by CellState value; FREE is transparent.
GRID_CELL_RGBA = (
    (0, 0, 0, 0),  # FREE
    (52, 211, 153, 110),  # SAME_NET
    (239, 68, 68, 120),  # FOREIGN_NET
    (148, 163, 184, 120),  # BLOCKED (holes)
    (217, 70, 239, 120),  # KEEPOUT
    (250, 204, 21, 130),  # EDGE
    (15, 23, 42, 150),  # OUTSIDE_BOARD
    (255, 140, 0, 140),  # UNKNOWN
)

_LAYER_COLORS = {
    FRONT_COPPER: QColor("#d04a3f"),
    BACK_COPPER: QColor("#3f86d0"),
    EDGE_CUTS: OUTLINE_COLOR,
}
_INNER_PALETTE = ("#d9921a", "#3cb371", "#b05fd0", "#40c0c0", "#d0609a", "#9acd32")
COPPER_ALPHA = 205


def layer_color(name: str) -> QColor:
    if name in _LAYER_COLORS:
        return QColor(_LAYER_COLORS[name])
    inner = inner_layer_index(name)
    if inner is not None:
        return QColor(_INNER_PALETTE[(inner - 1) % len(_INNER_PALETTE)])
    return QColor("#8899aa")


def copper_color(name: str, alpha: int = COPPER_ALPHA) -> QColor:
    c = layer_color(name)
    c.setAlpha(alpha)
    return c


# ------------------------------------------------------------------ widget theme
_DARK = {
    "window": "#1b1f24",
    "base": "#14181d",
    "alt": "#1f242b",
    "text": "#d7dde5",
    "muted": "#8b95a3",
    "button": "#252b33",
    "border": "#2f3640",
    "highlight": "#2f6fb3",
    "highlight_text": "#ffffff",
}
_LIGHT = {
    "window": "#f3f4f6",
    "base": "#ffffff",
    "alt": "#f7f8fa",
    "text": "#1f2328",
    "muted": "#5b6470",
    "button": "#e8eaee",
    "border": "#c9ced6",
    "highlight": "#2f6fb3",
    "highlight_text": "#ffffff",
}


def muted_text_color(theme: Theme) -> QColor:
    return QColor((_DARK if theme is Theme.DARK else _LIGHT)["muted"])


def apply_theme(app: QApplication, theme: Theme) -> None:
    c = _DARK if theme is Theme.DARK else _LIGHT
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(c["window"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(c["base"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(c["alt"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(c["button"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["base"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(c["highlight"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(c["highlight_text"]))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["muted"]))
    app.setPalette(pal)

    font = QFont(app.font())
    if font.pointSizeF() < 9.5:
        font.setPointSizeF(9.5)
    app.setFont(font)

    app.setStyleSheet(f"""
        QMainWindow::separator {{ background: {c["border"]}; width: 1px; height: 1px; }}
        QDockWidget {{ titlebar-close-icon: none; }}
        QDockWidget::title {{
            background: {c["alt"]}; padding: 5px 8px; border-bottom: 1px solid {c["border"]};
        }}
        QToolBar {{ border: none; border-bottom: 1px solid {c["border"]}; spacing: 4px;
                    padding: 3px; }}
        QStatusBar {{ border-top: 1px solid {c["border"]}; }}
        QStatusBar QLabel {{ padding: 0 8px; color: {c["muted"]}; }}
        QHeaderView::section {{
            background: {c["alt"]}; border: none; border-right: 1px solid {c["border"]};
            border-bottom: 1px solid {c["border"]}; padding: 4px 6px;
        }}
        QTableView, QTreeView, QTreeWidget, QListWidget, QPlainTextEdit {{
            border: 1px solid {c["border"]}; gridline-color: {c["border"]};
        }}
        QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
            border: 1px solid {c["border"]}; border-radius: 3px; padding: 3px 5px;
            background: {c["base"]};
        }}
        QPushButton {{
            border: 1px solid {c["border"]}; border-radius: 3px; padding: 4px 10px;
            background: {c["button"]};
        }}
        QPushButton:hover {{ border-color: {c["highlight"]}; }}
        QPushButton:disabled {{ color: {c["muted"]}; }}
        QLabel[role="muted"] {{ color: {c["muted"]}; }}
        QLabel[role="banner"] {{
            background: {c["alt"]}; border: 1px solid {c["border"]}; border-radius: 3px;
            padding: 8px; color: {c["muted"]};
        }}
        QGroupBox {{ border: 1px solid {c["border"]}; border-radius: 4px; margin-top: 12px;
                     padding-top: 6px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
        """)
