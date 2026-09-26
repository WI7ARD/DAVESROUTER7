"""Help ▸ Guides (F1): step-by-step in-app guides for every feature.

Each guide is short HTML plus optional "Do it" buttons that open the relevant
dialog or run the action, so the guide and the feature are one click apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from pcbrouter.ui.main_window import MainWindow


@dataclass(frozen=True)
class Guide:
    key: str
    title: str
    html: str
    #: (button label, MainWindow method name)
    actions: tuple[tuple[str, str], ...] = field(default=())


def _steps(*items: str) -> str:
    return "<ol>" + "".join(f"<li>{i}</li>" for i in items) + "</ol>"


GUIDES: tuple[Guide, ...] = (
    Guide(
        "start",
        "Getting started",
        "<h2>Getting started</h2>"
        "<p>AI PCB Router opens a KiCad board, routes it with a deterministic router "
        "(the same input always gives the same result), checks every route against "
        "your design rules, and lets you review before anything is kept.</p>"
        + _steps(
            "<b>File ▸ Open Board…</b> (Ctrl+O) and pick a <code>.kicad_pcb</code>. "
            "Your file is only read — it is never changed unless you export.",
            "Select a net (click a pad or pick it in the <b>Nets</b> panel) and press "
            "<b>R</b> to route it, or <b>Router ▸ Route Board</b> (Ctrl+Shift+R) for all "
            "nets.",
            "Review the proposal in <b>Route Review</b> / <b>Routing Jobs</b> and click "
            "<b>Accept</b>. Undo with Ctrl+Z.",
            "<b>File ▸ Export Routed Board…</b> (Ctrl+E) writes a new "
            "<code>name_routed.kicad_pcb</code>; open it in KiCad and run KiCad's DRC.",
        )
        + "<p>Passing the app's checks does <b>not</b> mean a board is ready to "
        "manufacture: always review it in KiCad and against your fabricator's rules.</p>",
        (("Open a board…", "open_board_dialog"),),
    ),
    Guide(
        "route_net",
        "Route one net",
        "<h2>Route one net</h2>"
        + _steps(
            "Click a pad or track of the net, or select it in the <b>Nets</b> panel.",
            "Press <b>R</b> (Router ▸ Route Selected Net).",
            "A card over the board shows progress: phase, backend (CPU/GPU), elapsed "
            "time. <b>Cancel Routing</b> stops it; nothing is changed.",
            "In <b>Route Review</b>, flip through the candidates (Best / Alternatives). "
            "Each shows length, vias, bends and its validation.",
            "<b>Accept</b> adds it to the working board; <b>Reject</b> discards it.",
        )
        + "<p>Settings ▸ Routing sets candidates per net and the search time limit; "
        "Settings ▸ Geometry sets the routing grid (finer = slower, fits tighter gaps). "
        "Per-net width/layers: <b>Router ▸ Net Routing Constraints…</b>.</p>",
    ),
    Guide(
        "route_board",
        "Route the whole board",
        "<h2>Route the whole board</h2>"
        + _steps(
            "<b>Router ▸ Route Board</b> (Ctrl+Shift+R).",
            "Watch the routing card: <i>Routing net 37 of 146</i>, pass number, rip-ups. "
            "The window stays usable — move it, pan the board, read the log.",
            "Pause / Resume / Cancel in <b>Routing Jobs</b>, or Cancel on the card.",
            "When it finishes, Routing Jobs lists every net: accept all, or select "
            "nets and accept only those.",
        )
        + "<p>Strategy, passes and rip-up are in Settings ▸ Routing. Rip-up never touches "
        "copper from your file or locked copper. The job has a time budget; if a net "
        "fails, the reason is shown (no path, via limit, layer restriction…).</p>",
        (("Route Board", "_guide_route_board"),),
    ),
    Guide(
        "freerouting",
        "Route with Freerouting (recommended for real boards)",
        "<h2>Route with Freerouting</h2>"
        "<p>Freerouting is the mature open-source autorouter many KiCad users rely on. "
        "The app runs it as a separate program, then checks every net it routed with the "
        "exact validator before you accept anything.</p>"
        + _steps(
            "Install KiCad 7–10 (its Python converts the board to Freerouting's format).",
            "<b>Tools ▸ Set Up Freerouting…</b> → <b>Open download page</b> → run the "
            "Windows installer (<code>freerouting-…-windows-x64.msi</code>, includes Java) "
            "→ <b>Check again</b>. Or <b>Download .jar</b> + <b>Install Java</b>.",
            "Open your board, then <b>Test on open board</b> (one pass, nothing changes).",
            "<b>Router ▸ Route Board with Freerouting…</b>. Progress (pass, unrouted "
            "count) shows in the routing overlay; Cancel stops Freerouting.",
            "Review the result in <b>Routing Jobs</b>: accept all or per net. Nets the "
            "validator rejects are listed with the reason and are never added.",
            "<b>File ▸ Export Routed Board…</b> writes a new file (the source is untouched).",
        )
        + "<p>Command line: <code>pcbrouter --freeroute board.kicad_pcb --output "
        "routed.kicad_pcb</code>. Existing copper is locked, so Freerouting only adds "
        "routes. Freerouting is GPL-3.0 and is not included in this app.</p>",
        (
            ("Set Up Freerouting…", "open_freerouting_setup"),
            ("Route with Freerouting", "_guide_freeroute"),
        ),
    ),
    Guide(
        "workbench",
        "Review, locks and constraints",
        "<h2>Review, locks, constraints</h2>"
        "<ul><li><b>View ▸ Board View</b>: Working (all copper), Original (your file), "
        "Overlay, Difference (only what the router added).</li>"
        "<li><b>Edit ▸ Lock / Unlock Selected</b>: locked copper and nets are never "
        "rerouted or ripped up. <b>Lock Region…</b> keeps new routes out of an area.</li>"
        "<li><b>Router ▸ Net Routing Constraints…</b>: width, allowed layers, via limit "
        "for one net. <b>Add Routing Corridor…</b>: prefer or avoid an area (soft).</li>"
        "<li><b>Reroute Selected Section</b>: select a router-made track and reroute just "
        "that part.</li><li><b>Explain Route Vias</b> tells you why each via exists.</li>"
        "<li>Optimise a routed net: Router ▸ Optimize Selected Net (shorter, fewer vias, "
        "fewer bends, more clearance, merge segments) — one undoable step.</li></ul>",
    ),
    Guide(
        "checks",
        "Checks: Internal Geometry Check and KiCad DRC",
        "<h2>Checks</h2>"
        "<p><b>Internal Geometry Check</b> (Tools / Geometry menu) is the app's own exact "
        "check of clearances, widths, vias and holes against the rules it read from your "
        "project. Every proposed route is checked before you can accept it.</p>"
        "<p><b>KiCad DRC</b> is KiCad's own checker. If KiCad 7+ is installed, turn on "
        "Settings ▸ Export ▸ <i>Run KiCad DRC</i>; the result appears after each export, "
        "labelled 'KiCad DRC'.</p><p>If a rule is unknown the router refuses rather than "
        "guess (RULE_UNKNOWN). See <b>Routing Rules</b> to inspect what was read.</p>",
    ),
    Guide(
        "export",
        "Export, sessions and recovery",
        "<h2>Export, sessions, recovery</h2>"
        + _steps(
            "<b>File ▸ Export Routed Board…</b> (Ctrl+E): writes a <i>new</i> file "
            "(default <code>name_routed.kicad_pcb</code>). The export only adds tracks "
            "and vias, re-reads the result to check it, and writes atomically.",
            "If the Internal Geometry Check finds errors, you are asked before an "
            "<b>UNVERIFIED</b> copy is written (it is labelled as such).",
            "<b>Save Session… / Load Session…</b> keep your routed copper, locks and "
            "constraints to continue later.",
            "If the app closes unexpectedly, reopening the same board offers to "
            "<b>recover</b> your work.",
        )
        + "<p>Overwriting the original board is off by default (Settings ▸ Export); when "
        "enabled a backup is made first.</p>",
        (("Export Routed Board…", "_guide_export"),),
    ),
    Guide(
        "gpu",
        "GPU routing (Intel Iris Xe / Arc, NVIDIA)",
        "<h2>GPU routing</h2>"
        + _steps(
            "<b>Tools ▸ Set Up GPU…</b>: shows your GPU and what is missing. If it "
            "does not find your GPU, choose Intel or NVIDIA under <i>GPU maker</i>.",
            "Update the graphics driver (Intel: intel.com / Driver & Support Assistant).",
            "Click <b>Install GPU support</b> (installs <code>dpnp</code> for Intel, "
            "<code>cupy</code> for NVIDIA).",
            "Click <b>Use GPU for routing</b>, open a board, then <b>Test GPU on this "
            "board</b>.",
        )
        + "<p>The routing card always shows <i>Requested</i> vs <i>Selected</i> backend, "
        "so you can see whether the GPU really ran. <b>Auto</b> uses the GPU only for "
        "very large grids; on small boards the CPU is usually faster (GPU work has "
        "start-up costs). Every GPU route is checked by the same exact validator. The "
        "Setup.exe build cannot install GPU libraries — use <code>setup_gpu.bat</code>.</p>",
        (("Set Up GPU…", "open_gpu_setup"),),
    ),
    Guide(
        "ollama",
        "Free local AI (Ollama)",
        "<h2>Free local AI with Ollama</h2>"
        + _steps(
            "Install Ollama from ollama.com (or run <code>setup_ollama.bat</code>) and "
            "start it once.",
            "<b>AI ▸ Set Up Local AI (Ollama)…</b> → pick a model (qwen2.5:7b for 16 GB "
            "RAM, qwen2.5:3b for 8 GB) → <b>Download model</b>.",
            "<b>Use this model</b>, then <b>Test</b>.",
            "Open the AI panel (Ctrl+I) and ask about your board.",
        )
        + "<p>No API key; nothing is sent to the internet. Local models are slower and "
        "less precise than large cloud models; the app validates every answer and the "
        "router, not the AI, decides geometry.</p>",
        (("Set Up Local AI…", "open_ollama_setup"),),
    ),
    Guide(
        "ai",
        "The AI engineering assistant",
        "<h2>AI assistant</h2>"
        "<ul><li>Open it with <b>AI ▸ AI Engineering Panel</b> (Ctrl+I). Configure a "
        "provider in <b>AI ▸ Configure AI Providers…</b> (OpenAI, Anthropic, or local via "
        "Ollama). Keys are stored in the OS credential store, never in files.</li>"
        "<li>Before sending, the privacy preview shows exactly what board data is shared "
        "(names can be anonymised).</li><li>The AI proposes commands (e.g. route these nets "
        "on inner layers). You approve them; the deterministic router executes them and "
        "every result is validated. The AI never edits your file or geometry.</li>"
        "<li>Autonomy (Settings ▸ AI): Advisory, Approval required (default), Batch "
        "approval.</li></ul>",
        (("Open AI panel", "show_ai_panel"), ("Configure providers…", "open_ai_settings")),
    ),
    Guide(
        "trouble",
        "Troubleshooting",
        "<h2>Troubleshooting</h2>"
        "<ul><li><b>Routing seems stuck</b>: the card shows elapsed time and 'last update'; "
        "the worker is alive while that clock runs. Cancel is always safe.</li>"
        "<li><b>A net fails</b>: read the reason in Route Review; try a finer grid, more "
        "layers, allowing vias, or unlocking a region.</li>"
        "<li><b>GPU not used</b>: the card says why (e.g. 'CPU fallback: dpnp not "
        "installed'). Use Tools ▸ Set Up GPU….</li>"
        "<li><b>The app closed unexpectedly</b>: send the files <code>crash.log</code> and "
        "<code>worker-crash.log</code> from the log folder (Windows: "
        "<code>%LOCALAPPDATA%\\AI PCB Router\\logs</code>), or use <b>Help ▸ Export "
        "Diagnostic Bundle…</b> (contains no board data and no keys).</li>"
        "<li>Logs are also in the <b>Logs</b> panel (View ▸ Logs).</li></ul>",
        (("Export Diagnostic Bundle…", "_guide_bundle"),),
    ),
    Guide(
        "keys",
        "Keyboard shortcuts",
        "<h2>Keyboard shortcuts</h2><table cellpadding=4>"
        + "".join(
            f"<tr><td><b>{k}</b></td><td>{v}</td></tr>"
            for k, v in (
                ("F1", "Guides"),
                ("Ctrl+O", "Open board"),
                ("Ctrl+E", "Export routed board"),
                ("R", "Route selected net"),
                ("Ctrl+Shift+R", "Route board"),
                ("Ctrl+Z / Ctrl+Shift+Z", "Undo / Redo"),
                ("Ctrl+I", "AI panel"),
                ("Ctrl+= / Ctrl+-", "Zoom in / out"),
                ("G", "Grid"),
                ("Ctrl+,", "Settings"),
                ("Ctrl+Q", "Quit"),
            )
        )
        + "</table>",
    ),
)


class GuideDialog(QDialog):
    def __init__(self, window: MainWindow, topic: str = "start") -> None:
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Guides")
        self.resize(900, 600)
        self.topics = QListWidget()
        self.topics.setAccessibleName("Guide topics")
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        self.buttons = QHBoxLayout()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.text, 1)
        rl.addLayout(self.buttons)
        split = QSplitter()
        split.addWidget(self.topics)
        split.addWidget(right)
        split.setSizes([220, 680])
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        for g in GUIDES:
            item = QListWidgetItem(g.title)
            item.setData(Qt.ItemDataRole.UserRole, g.key)
            self.topics.addItem(item)
        self.topics.currentRowChanged.connect(self._show)
        self.show_topic(topic)

    def show_topic(self, key: str) -> None:
        idx = next((i for i, g in enumerate(GUIDES) if g.key == key), 0)
        self.topics.setCurrentRow(idx)
        self._show(idx)

    def _show(self, row: int) -> None:
        if not 0 <= row < len(GUIDES):
            return
        g = GUIDES[row]
        self.text.setHtml(g.html)
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for label, method in g.actions:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, m=method: self._run(m))
            self.buttons.addWidget(b)
        self.buttons.addStretch(1)

    def _run(self, method: str) -> None:
        fn: Any = getattr(self.w, method, None)
        if callable(fn):
            fn()
