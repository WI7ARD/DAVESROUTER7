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
from dataclasses import dataclass
from pathlib import Path

from pcbrouter.domain.board import Board
from pcbrouter.history.snapshot import MetadataSnapshotStore, SnapshotStore
from pcbrouter.kicad.loader import LoadResult, load_board, sha256_of_file
from pcbrouter.project.workspace import Workspace, workspace_for

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectSession:
    """Everything known about the open board."""

    source_path: Path
    source_sha256: str
    load_result: LoadResult
    workspace: Workspace
    opened_at: float

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
        workspace = workspace_for(result.path, self._workspace_base)
        self._session = ProjectSession(
            source_path=result.path,
            source_sha256=result.stats.sha256,
            load_result=result,
            workspace=workspace,
            opened_at=time.time(),
        )
        log.info(
            "project.open source=%s sha256=%s workspace=%s snapshots=%s (metadata only)",
            result.path, result.stats.sha256[:16], workspace.root, workspace.snapshots_dir,
        )  # fmt: skip
        return self._session

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
        return report
