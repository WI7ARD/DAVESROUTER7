"""Export commands (Stage 9): routed board → new .kicad_pcb, gated by DRC.

* :class:`ExportBoardCommand` writes ``<name>_routed.kicad_pcb`` (or another new
  file). It runs the Internal Geometry Check first; with errors the export is
  refused unless the user explicitly exports an *unverified* copy, which is then
  labelled as such in the status and the provenance sidecar.
* :class:`OverwriteSourceCommand` replaces the source board (backup first). It is a
  ``modifies_board`` command, so the bus refuses it while the application is in its
  default read-only-source policy.

Wording is deliberate: "Internal checks passed" / "KiCad DRC passed" — never
"manufacturing ready" (fabrication limits depend on the manufacturer).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult
from pcbrouter.kicad.writer import ExportReport, ExportStatus, export_board


@dataclass
class ExportBoardCommand(BaseCommand):
    out_path: Path
    allow_unverified: bool = False
    run_kicad_drc: bool = False
    name: ClassVar[str] = "export_board"
    overwrite: ClassVar[bool] = False

    def execute(self, ctx: CommandContext) -> CommandResult:
        project = ctx.project
        session, working, engine = project.session, project.working, project.engine
        if session is None or working is None or engine is None:
            return CommandResult.fail("Open a board first.")
        drc = engine.run_drc()
        errors, warnings = len(drc.errors), len(drc.warnings)
        verified = errors == 0
        verification = (
            f"Internal checks passed ({warnings} warning(s))"
            if verified
            else f"UNVERIFIED — Internal Geometry Check found {errors} error(s)"
        )
        if not verified and not self.allow_unverified:
            report = ExportReport(
                ExportStatus.EXPORT_BLOCKED_DRC,
                messages=[
                    verification + "; export an unverified copy "
                    "explicitly or fix the errors first"
                ],
            )
            return CommandResult(False, report.summary(), report)
        report = export_board(
            session.source_path,
            session.source_sha256,
            working.source,
            working.board,
            self.out_path,
            overwrite_source=self.overwrite,
            backup_dir=session.workspace.root / "backups",
            provenance={k: v.value for k, v in working.provenance.items()},
            verification=(verified, verification),
            extra_metadata={
                "internal_drc": drc.summary(),
                "board_fingerprint": working.board.fingerprint,
            },
        )
        if report.ok and self.run_kicad_drc and report.path is not None:
            from pcbrouter.kicad.kicad_cli import run_kicad_drc

            kicad = run_kicad_drc(report.path)
            report.messages.append(kicad.summary())
            if kicad.ran:
                report.verification += "; " + kicad.summary()
                report.verified = report.verified and kicad.passed
        return CommandResult(report.ok, report.summary(), report)

    def describe(self) -> str:
        return f"export routed board to {self.out_path.name}"


@dataclass
class OverwriteSourceCommand(ExportBoardCommand):
    """Replace the source file (a backup copy is made first)."""

    name: ClassVar[str] = "overwrite_source_board"
    modifies_board: ClassVar[bool] = True
    overwrite: ClassVar[bool] = True


@dataclass
class SaveSessionCommand(BaseCommand):
    """Write the working session (added copper, locks, constraints) to a JSON file."""

    path: Path | None = None  # None = the crash-recovery file in the workspace
    name: ClassVar[str] = "save_session"

    def execute(self, ctx: CommandContext) -> CommandResult:
        from pcbrouter.project.session_store import (
            SessionError,
            recovery_path,
            save_session,
            session_data,
        )

        session, working = ctx.project.session, ctx.project.working
        if session is None or working is None:
            return CommandResult.fail("Open a board first.")
        target = self.path or recovery_path(session.workspace.root)
        try:
            data = session_data(working, session.source_path.name, session.source_sha256)
            save_session(target, data)
        except (OSError, SessionError) as exc:
            return CommandResult.fail(f"Session not saved: {exc}")
        return CommandResult.ok(f"Session saved to {target.name}", target)

    def describe(self) -> str:
        return "save working session"


@dataclass
class RestoreSessionCommand(BaseCommand):
    """Apply a saved/recovered session to the open board (validated, undoable)."""

    path: Path
    name: ClassVar[str] = "restore_session"

    def execute(self, ctx: CommandContext) -> CommandResult:
        from pcbrouter.commands.route_commands import WorkingUndoAction
        from pcbrouter.project.session_store import SessionError, load_session, restore_session
        from pcbrouter.routing.working_board import CommitError

        session, working = ctx.project.session, ctx.project.working
        if session is None or working is None:
            return CommandResult.fail("Open a board first.")
        try:
            data = load_session(self.path)
            commit = restore_session(working, data, session.source_sha256)
        except (OSError, SessionError, CommitError, KeyError, TypeError, ValueError) as exc:
            return CommandResult.fail(f"Session not restored: {exc}")
        if commit is not None:
            ctx.history.push(WorkingUndoAction(working, commit), already_applied=True)
            return CommandResult.ok(f"Recovered session: {commit.diff_summary()}", commit)
        return CommandResult.ok("Recovered session (locks/constraints only)", None)

    def describe(self) -> str:
        return f"restore session from {self.path.name}"
