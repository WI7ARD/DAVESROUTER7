"""Owns the currently open board and enforces the read-only safety model.

Stage 1 guarantees:

* the source ``.kicad_pcb`` is only ever opened in binary read mode;
* its SHA-256 is recorded at open time and re-checked at close time;
* nothing is written anywhere near the source file.

Stage 9 will add safe writing: write to a temp file in the workspace, validate by
re-parsing, snapshot the previous version, then atomically replace — and only on
explicit user action.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pcbrouter.domain.board import Board
from pcbrouter.history.snapshot import MetadataSnapshotStore, SnapshotStore
from pcbrouter.kicad.loader import LoadResult, load_board, sha256_of_file
from pcbrouter.kicad.rule_adapter import ProjectRuleData, load_project_rules
from pcbrouter.project.workspace import Workspace, workspace_for

if TYPE_CHECKING:
    from pcbrouter.board_engine import BoardEngine
    from pcbrouter.rules.overrides import RuleOverrides

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectSession:
    """Everything known about the open board."""

    source_path: Path
    source_sha256: str
    load_result: LoadResult
    workspace: Workspace
    opened_at: float
    #: Unique per open; lets AI responses be matched to the board they were made for.
    session_id: str = field(default_factory=lambda: f"board-{uuid.uuid4().hex[:12]}")
    #: Rules read (read-only) from the .kicad_pro / .kicad_dru next to the board.
    project_rules: ProjectRuleData = field(default_factory=ProjectRuleData)

    @property
    def board(self) -> Board:
        return self.load_result.board

    @property
    def name(self) -> str:
        return self.source_path.name


@dataclass(frozen=True, slots=True)
class CloseReport:
    source_path: Path
    #: True: unchanged; False: changed on disk (by another program); None: unreadable.
    source_unchanged: bool | None


class ProjectManager:
    def __init__(
        self,
        workspace_base: Path | None = None,
        snapshot_store: SnapshotStore | None = None,
    ) -> None:
        self._workspace_base = workspace_base
        self._session: ProjectSession | None = None
        self.snapshots: SnapshotStore = snapshot_store or MetadataSnapshotStore()
        self._engine: BoardEngine | None = None
        self._ai_overrides: RuleOverrides | None = None
        #: Conservative rule handling (Stage 3). ON by default; see docs/rules_engine.md.
        self.conservative_rules = True

    @property
    def session(self) -> ProjectSession | None:
        return self._session

    @property
    def board(self) -> Board | None:
        return self._session.board if self._session else None

    @property
    def is_open(self) -> bool:
        return self._session is not None

    def open_board(self, path: Path | str) -> ProjectSession:
        """Load a board read-only. The previous board stays open if loading fails."""
        result = load_board(path)
        if self._session is not None:
            self.close_board()
        self._engine = None
        self._ai_overrides = None
        workspace = workspace_for(result.path, self._workspace_base)
        self._session = ProjectSession(
            source_path=result.path,
            source_sha256=result.stats.sha256,
            load_result=result,
            workspace=workspace,
            opened_at=time.time(),
            project_rules=load_project_rules(result.path),
        )
        log.info(
            "project.open source=%s sha256=%s workspace=%s snapshots=%s (metadata only)",
            result.path, result.stats.sha256[:16], workspace.root, workspace.snapshots_dir,
        )  # fmt: skip
        return self._session

    # ------------------------------------------------------------ Stage 3 engine
    @property
    def engine(self) -> BoardEngine | None:
        """The deterministic geometry/rule engine for the open board (lazy)."""
        if self._session is None:
            return None
        if self._engine is None:
            from pcbrouter.board_engine import BoardEngine, EngineConfig

            self._engine = BoardEngine(
                self._session.board,
                self._session.project_rules,
                self.effective_overrides(),
                EngineConfig(conservative=self.conservative_rules),
            )
        return self._engine

    def manual_overrides(self) -> RuleOverrides:
        from pcbrouter.rules.overrides import RuleOverrides, load_overrides

        if self._session is None:
            return RuleOverrides()
        return load_overrides(self._session.workspace.root)

    def effective_overrides(self) -> RuleOverrides:
        manual = self.manual_overrides()
        return manual.combined(self._ai_overrides) if self._ai_overrides else manual

    def save_manual_overrides(self, overrides: RuleOverrides) -> None:
        from pcbrouter.rules.overrides import save_overrides

        if self._session is None:
            return
        save_overrides(self._session.workspace.root, overrides)
        self._refresh_engine_rules()

    def set_ai_overrides(self, overrides: RuleOverrides | None) -> None:
        """Constraints the user approved in the AI planner (Stage 2 proposals)."""
        self._ai_overrides = overrides
        self._refresh_engine_rules()

    def set_conservative_rules(self, enabled: bool) -> None:
        if enabled != self.conservative_rules:
            self.conservative_rules = enabled
            self._engine = None

    def _refresh_engine_rules(self) -> None:
        if self._engine is not None:
            self._engine = self._engine.with_overrides(self.effective_overrides())

    def verify_source_unchanged(self) -> bool | None:
        if self._session is None:
            return None
        try:
            return sha256_of_file(self._session.source_path) == self._session.source_sha256
        except OSError:
            return None

    def close_board(self) -> CloseReport | None:
        if self._session is None:
            return None
        unchanged = self.verify_source_unchanged()
        report = CloseReport(self._session.source_path, unchanged)
        if unchanged is False:
            # Not an error of ours: KiCad (or the user) may have saved the file.
            log.warning("project.close source=%s changed_externally=true", report.source_path)
        else:
            log.info("project.close source=%s unchanged=%s", report.source_path, unchanged)
        self._session = None
        self._engine = None
        self._ai_overrides = None
        return report
