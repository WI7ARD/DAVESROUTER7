"""First-run welcome: what the app does, what it supports, where files go.

Shown once on the first launch (``settings.welcome_shown``) and any time from
Help ▸ Welcome. Non-modal: the main window stays usable behind it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QTextBrowser,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow

WELCOME_HTML = """
<h2>Welcome to AI PCB Router</h2>
<p>Automatic, rule-checked routing for KiCad boards. Everything runs on this computer;
no network connection is needed (AI assistance is optional).</p>
<h3>Workflow</h3>
<ol>
<li><b>Open Board</b> (File ▸ Open Board…, Ctrl+O) — a <code>.kicad_pcb</code>. Keep the
board's <code>.kicad_pro</code> (and <code>.kicad_dru</code>, if any) next to it: the design
rules are read from there. Without them the router refuses to guess widths and
clearances.</li>
<li><b>Configure</b> — the Route panel's <b>Speed / Accuracy</b> toggle. Speed routes
quickly on a coarser grid; Accuracy searches harder for shorter routes with fewer vias.
Parallel routing (Settings ▸ Routing) is automatic.</li>
<li><b>Route</b> — Router ▸ Route Board (Ctrl+Shift+R). The window stays usable; the
routing card shows the phase, net n of N, elapsed time and a Cancel button.</li>
<li><b>Review &amp; validate</b> — results appear in Routing Jobs. Accept all nets or
some. Every piece of copper has already passed the exact clearance/width checker; run
Tools ▸ Internal Geometry Check for a full report.</li>
<li><b>Save</b> — File ▸ Export Routed Board… writes a <b>new</b> file
(<code>&lt;name&gt;_routed.kicad_pcb</code> next to the original). The original board is
never changed unless you enable overwriting in Settings ▸ Export.</li>
</ol>
<h3>Supported</h3>
<ul>
<li>KiCad 6, 7, 8, 9 and 10 board files (exports: KiCad 6–10).</li>
<li>2-layer and 4-layer boards (F.Cu, In1.Cu, In2.Cu, B.Cu); through vias only.</li>
<li>Rules: net classes, track width, clearance, via size/drill, board-edge clearance and
supported custom rules from <code>.kicad_dru</code>. Rules the app cannot interpret are
listed, never silently ignored.</li>
</ul>
<h3>Important</h3>
<p><b>A routed board still needs your engineering and manufacturing review</b>: run
KiCad's own DRC, refill zones (B in KiCad) and check critical nets before ordering
boards. "Internal checks passed" is not the same as a KiCad DRC; the export summary
says which checks ran.</p>
<p>Help ▸ Guides (F1) has step-by-step guides for every feature.</p>
"""


class WelcomeDialog(QDialog):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Welcome")
        self.resize(640, 620)
        text = QTextBrowser()
        text.setHtml(WELCOME_HTML)
        self.show_again = QCheckBox("Show this at startup")
        self.show_again.setChecked(not window.settings.welcome_shown)
        buttons = QDialogButtonBox()
        open_btn = buttons.addButton("Open a Board…", QDialogButtonBox.ButtonRole.AcceptRole)
        guides = buttons.addButton("Guides", QDialogButtonBox.ButtonRole.HelpRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        open_btn.clicked.connect(self._open_board)
        guides.clicked.connect(lambda: self.w.open_guides())
        close.clicked.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(text, 1)
        layout.addWidget(self.show_again)
        layout.addWidget(buttons)

    def _open_board(self) -> None:
        self.close()
        self.w.open_board_dialog()

    def done(self, result: int) -> None:
        self._remember()
        super().done(result)

    def closeEvent(self, event: object) -> None:
        self._remember()
        super().closeEvent(event)  # type: ignore[arg-type]

    def _remember(self) -> None:
        shown = not self.show_again.isChecked()
        if self.w.settings.welcome_shown != shown:
            self.w.settings.welcome_shown = shown
            self.w.save_settings()
