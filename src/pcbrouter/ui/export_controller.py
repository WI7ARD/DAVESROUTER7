"""Stage 9 UI: export the routed board, sessions, crash recovery, diagnostics.

* File ▸ Export Routed Board… (Ctrl+E) → ``<name>_routed.kicad_pcb`` by default,
  gated by the Internal Geometry Check; an *unverified* export needs an explicit
  second confirmation and is labelled as such.
* File ▸ Overwrite Source Board… only when Settings ▸ Export allows it (the command
  bus is read-only otherwise); a backup is made first.
* File ▸ Save Session… / Load Session… (added copper, locks, constraints).
* Crash recovery: after each working-board change the session is written to the
  workspace; on the next open of the *same* file (same SHA-256) recovery is
  offered; a clean close deletes it.
* Help ▸ Export Diagnostic Bundle… (no PCB, no keys).

Dialogs go through small hooks (``ask``, ``save_path``, ``open_path``, ``inform``) so
tests can drive every path without modal windows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

from pcbrouter.commands import (
    OverwriteSourceCommand,
    RestoreSessionCommand,
    SaveSessionCommand,
)
from pcbrouter.jobs.protocol import ExportJob, JobDone, WorkingSnapshot
from pcbrouter.kicad.writer import ExportReport, ExportStatus, default_export_path
from pcbrouter.project.session_store import SessionError, load_session, recovery_path

if TYPE_CHECKING:
    from pcbrouter.routing.working_board import Commit, WorkingBoard
    from pcbrouter.ui.main_window import MainWindow

log = logging.getLogger(__name__)

AUTOSAVE_DELAY_MS = 1500
SESSION_FILTER = "Router session (*.pcbrouter-session.json *.json)"


class ExportController:
    def __init__(self, window: MainWindow) -> None:
        self.w = window
        self._working: WorkingBoard | None = None
        self.last_export: Any = None
        self.export_running = False
        #: working-board fingerprint last written to disk (export or saved session)
        self._saved_fingerprint: str | None = None
        # dialog hooks (replaced in tests)
        self.ask: Callable[[str, str], bool] = self._ask
        self.save_path: Callable[[str, Path, str], Path | None] = self._save_path
        self.open_path: Callable[[str, Path, str], Path | None] = self._open_path
        self.inform: Callable[[str, str], None] = self._inform
        self._autosave = QTimer(window)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(AUTOSAVE_DELAY_MS)
        self._autosave.timeout.connect(self.autosave_now)

        self.act_export = QAction("&Export Routed Board…", window)
        self.act_export.setShortcut(QKeySequence("Ctrl+E"))
        self.act_export.setStatusTip(
            "Write the working board to a NEW .kicad_pcb (default <name>_routed); the "
            "source file is never modified"
        )
        self.act_export.triggered.connect(lambda: self.export_routed())
        self.act_overwrite = QAction("Overwrite &Source Board…", window)
        self.act_overwrite.setStatusTip(
            "Replace the opened .kicad_pcb (backup first). Disabled unless allowed in "
            "Settings ▸ Export"
        )
        self.act_overwrite.triggered.connect(lambda: self.overwrite_source())
        self.act_save_session = QAction("Save &Session…", window)
        self.act_save_session.setStatusTip("Save routed copper, locks and constraints")
        self.act_save_session.triggered.connect(lambda: self.save_session())
        self.act_load_session = QAction("&Load Session…", window)
        self.act_load_session.setStatusTip("Re-apply a saved session to the same board file")
        self.act_load_session.triggered.connect(lambda: self.load_session())
        self.act_bundle = QAction("Export &Diagnostic Bundle…", window)
        self.act_bundle.setStatusTip(
            "Zip of versions, hardware detection, settings and the log tail — no PCB "
            "data, no API keys"
        )
        self.act_bundle.triggered.connect(lambda: self.export_bundle())

    # ------------------------------------------------------------ setup
    def install(self, file_menu: QMenu, help_menu: QMenu, before: QAction) -> None:
        for act in (self.act_export, self.act_overwrite):
            file_menu.insertAction(before, act)
        file_menu.insertSeparator(before)
        for act in (self.act_save_session, self.act_load_session):
            file_menu.insertAction(before, act)
        file_menu.insertSeparator(before)
        help_menu.addAction(self.act_bundle)
        self.apply_settings()
        self.update_actions()

    def apply_settings(self) -> None:
        """The command bus stays read-only unless overwriting the source is allowed."""
        self.w.bus.read_only = not self.w.settings.export.allow_overwrite_source
        self.update_actions()

    def update_actions(self) -> None:
        has = self.project.session is not None
        route_jobs = getattr(self.w, "route_jobs", None)
        idle = has and not (route_jobs is not None and route_jobs.busy)
        self.act_export.setEnabled(idle)
        for act in (self.act_save_session, self.act_load_session):
            act.setEnabled(idle)
        self.act_overwrite.setEnabled(idle and self.w.settings.export.allow_overwrite_source)

    @property
    def project(self) -> Any:
        return self.w.bus.context.project

    # ------------------------------------------------------------ board lifecycle
    def on_board_changed(self) -> None:
        working = self.project.working
        if working is not self._working:
            self._working = working
            self._saved_fingerprint = None
            if working is not None:
                working.subscribe(self._on_working_changed)
                self.offer_recovery()
        self.update_actions()

    def _on_working_changed(self, _working: WorkingBoard, _commit: Commit | None) -> None:
        if self.w.settings.export.autosave_recovery:
            self._autosave.start()

    def autosave_now(self) -> bool:
        session, working = self.project.session, self.project.working
        if session is None or working is None:
            return False
        path = recovery_path(session.workspace.root)
        if not working.modified and not working.lock_state() and not working.net_constraints:
            path.unlink(missing_ok=True)
            return False
        res = self.w.bus.dispatch(SaveSessionCommand(None))
        if not res.success:
            log.warning("recovery.autosave_failed %s", res.message)
        return res.success

    def clean_close(self) -> None:
        """Called before a deliberate close. The recovery file is deleted when the
        routed work is safe on disk (exported/saved, or nothing routed); otherwise
        it is refreshed and kept, so unexported routing is offered on the next open
        instead of being lost silently."""
        self._autosave.stop()
        session, working = self.project.session, self.project.working
        if session is None:
            return
        if (
            working is not None
            and working.modified
            and (working.fingerprint != self._saved_fingerprint)
        ):
            self.autosave_now()
            log.info("recovery.kept reason=unexported_routing")
            return
        recovery_path(session.workspace.root).unlink(missing_ok=True)

    def offer_recovery(self) -> bool:
        session = self.project.session
        if session is None:
            return False
        path = recovery_path(session.workspace.root)
        if not path.exists():
            return False
        try:
            data = load_session(path)
        except SessionError as exc:
            log.warning("recovery.unreadable %s", exc)
            return False
        if (data.get("source") or {}).get("sha256") != session.source_sha256:
            log.info("recovery.ignored reason=source_changed")
            return False
        n = len(data.get("added_tracks", [])) + len(data.get("added_vias", []))
        if not self.ask(
            "Recover unsaved routing?",
            f"The last session for {session.source_path.name} ended without closing "
            f"cleanly. Recover {n} routed object(s), locks and constraints? The copper "
            "is re-validated; you can undo the recovery.",
        ):
            path.unlink(missing_ok=True)
            return False
        res = self.w.bus.dispatch(RestoreSessionCommand(path))
        self.w.statusBar().showMessage(res.message, 10000)
        if not res.success:
            self.inform("Recovery failed", res.message)
        return res.success

    # ------------------------------------------------------------ export
    def export_routed(self, path: Path | None = None) -> bool:
        session = self.project.session
        if session is None:
            return False
        if path is None:
            path = self.save_path(
                "Export routed board (new file)",
                default_export_path(session.source_path),
                "KiCad board (*.kicad_pcb)",
            )
            if path is None:
                return False
        if path.resolve() == session.source_path.resolve():
            self.inform(
                "Export blocked",
                "That is the opened source board. Export to a new file, or use File ▸ "
                "Overwrite Source Board (must be enabled in Settings ▸ Export).",
            )
            return False
        return self._export(path, overwrite=False)

    def overwrite_source(self) -> bool:
        session = self.project.session
        if session is None:
            return False
        if not self.w.settings.export.allow_overwrite_source or self.w.bus.read_only:
            self.inform(
                "Overwrite disabled",
                self.w.bus.dispatch(OverwriteSourceCommand(session.source_path)).message,
            )
            return False
        if not self.ask(
            "Overwrite the source board?",
            f"{session.source_path.name} will be replaced by the routed board. A backup "
            "copy is written to the workspace first. Continue?",
        ):
            return False
        return self._export(session.source_path, overwrite=True)

    def _export(self, path: Path, overwrite: bool, allow_unverified: bool = False) -> bool:
        """Export in the routing worker (internal check, write + reload self-check,
        optional KiCad DRC): the GUI stays responsive. Returns True when the job was
        started; the outcome arrives in :meth:`_export_done` (``last_export``)."""
        session, working = self.project.session, self.project.working
        if session is None or working is None:
            return False
        job = ExportJob(
            WorkingSnapshot.from_working(working),
            session.source_path,
            session.source_sha256,
            path,
            session.workspace.root / "backups",
            allow_unverified=allow_unverified,
            run_kicad_drc=self.w.settings.export.run_kicad_drc,
            overwrite_source=overwrite,
        )
        fingerprint = working.fingerprint

        def done(d: JobDone) -> None:
            self._export_done(d, path, overwrite, fingerprint)

        if self.w.route_jobs.submit(job, done) is None:
            self.w.statusBar().showMessage("Another routing job is running.", 6000)
            return False
        self.export_running = True
        self.w.statusBar().showMessage(f"Exporting {path.name}…")
        return True

    def _export_done(self, done: JobDone, path: Path, overwrite: bool, fingerprint: str) -> None:
        self.export_running = False
        report = done.value
        if not isinstance(report, ExportReport):
            msg = done.error or f"export {done.status.lower()}; nothing was written"
            self.last_export = None
            self.w.statusBar().showMessage(f"Export failed: {msg}", 15000)
            if done.status != "CANCELED":
                self.inform(
                    "Export failed", msg + (f"\n\n{done.traceback}" if done.traceback else "")
                )
            return
        if report.status is ExportStatus.EXPORT_BLOCKED_DRC:
            if self.ask(
                "Export an UNVERIFIED board?",
                f"{report.summary()}\n\nThe exported file would contain these errors. Export "
                "it anyway, labelled UNVERIFIED?",
            ):
                self._export(path, overwrite, allow_unverified=True)
                return
            self.last_export = report
            self.w.statusBar().showMessage("Export cancelled (internal check errors).", 8000)
            return
        self.last_export = report
        self.w.statusBar().showMessage(report.summary(), 15000)
        if report.ok:
            working = self.project.working
            if working is not None and working.fingerprint == fingerprint:
                self._saved_fingerprint = fingerprint
        else:
            self.inform("Export failed", report.summary())

    # ------------------------------------------------------------ sessions
    def save_session(self, path: Path | None = None) -> bool:
        session = self.project.session
        if session is None:
            return False
        if path is None:
            default = session.source_path.with_name(
                session.source_path.stem + ".pcbrouter-session.json"
            )
            path = self.save_path("Save session", default, SESSION_FILTER)
            if path is None:
                return False
        res = self.w.bus.dispatch(SaveSessionCommand(path))
        self.w.statusBar().showMessage(res.message, 8000)
        if res.success and self.project.working is not None:
            self._saved_fingerprint = self.project.working.fingerprint
        if not res.success:
            self.inform("Session not saved", res.message)
        return res.success

    def load_session(self, path: Path | None = None) -> bool:
        session = self.project.session
        if session is None:
            return False
        if path is None:
            path = self.open_path("Load session", session.source_path.parent, SESSION_FILTER)
            if path is None:
                return False
        res = self.w.bus.dispatch(RestoreSessionCommand(path))
        self.w.statusBar().showMessage(res.message, 10000)
        if not res.success:
            self.inform("Session not loaded", res.message)
        return res.success

    # ------------------------------------------------------------ diagnostics
    def diagnostics_info(self) -> dict[str, Any]:
        from pcbrouter.compute.probe import probe_gpu
        from pcbrouter.kicad.kicad_cli import find_kicad_cli
        from pcbrouter.ui import dialogs

        settings = self.w.settings.model_dump(mode="json")
        settings.pop("recent_boards", None)  # file paths are not needed
        settings.pop("last_open_directory", None)
        settings.pop("window", None)
        gpu = probe_gpu()
        kicad = find_kicad_cli()
        session = self.project.session
        working = self.project.working
        return {
            "compute": dialogs.compute_info_text(self.w.compute),
            "gpu_probe": {
                "status": gpu.status,
                "library": gpu.library,
                "devices": list(gpu.devices),
                "reason": gpu.reason,
            },
            "kicad_cli": kicad.version if kicad else "not found",
            "settings": settings,
            "reproducibility": {
                "source_sha256": session.source_sha256 if session else None,
                "board_fingerprint": working.board.fingerprint if working else None,
                "history": [c.label for c in working.commits] if working else [],
            },
        }

    def export_bundle(self, path: Path | None = None) -> Path | None:
        from pcbrouter.app.diagnostic_bundle import build_bundle

        if path is None:
            path = self.save_path(
                "Export diagnostic bundle",
                Path.home() / "pcbrouter-diagnostics.zip",
                "Zip archive (*.zip)",
            )
            if path is None:
                return None
        try:
            out = build_bundle(path, self.diagnostics_info())
        except OSError as exc:
            self.inform("Bundle not written", str(exc))
            return None
        self.w.statusBar().showMessage(f"Diagnostic bundle written: {out}", 10000)
        return out

    # ------------------------------------------------------------ default dialogs
    def _ask(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self.w, title, text)
        return answer == QMessageBox.StandardButton.Yes

    def _save_path(self, title: str, default: Path, filt: str) -> Path | None:
        path, _ = QFileDialog.getSaveFileName(self.w, title, str(default), filt)
        return Path(path) if path else None

    def _open_path(self, title: str, start: Path, filt: str) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(self.w, title, str(start), filt)
        return Path(path) if path else None

    def _inform(self, title: str, text: str) -> None:
        QMessageBox.warning(self.w, title, text)
