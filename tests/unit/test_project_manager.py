from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from pcbrouter.kicad import MalformedBoardError
from pcbrouter.project import ProjectManager, workspace_for, workspace_key
from pcbrouter.utils.paths import data_dir


def test_open_records_source_and_workspace(fixture_path: Callable[[str], Path]) -> None:
    pm = ProjectManager()
    session = pm.open_board(fixture_path("two_components.kicad_pcb"))
    assert pm.is_open and pm.board is session.board
    assert session.source_path == fixture_path("two_components.kicad_pcb").resolve()
    assert len(session.source_sha256) == 64
    assert session.workspace.root.parent == data_dir() / "workspaces"
    assert session.workspace.snapshots_dir.name == "snapshots"
    assert not session.workspace.exists  # Stage 1 computes paths only; creates nothing


def test_failed_open_keeps_previous_board(fixture_path: Callable[[str], Path]) -> None:
    pm = ProjectManager()
    first = pm.open_board(fixture_path("traces.kicad_pcb"))
    with pytest.raises(MalformedBoardError):
        pm.open_board(fixture_path("malformed_unbalanced.kicad_pcb"))
    assert pm.session is first


def test_close_verifies_source(tmp_path: Path, fixture_path: Callable[[str], Path]) -> None:
    copy = tmp_path / "copy.kicad_pcb"
    shutil.copy(fixture_path("traces.kicad_pcb"), copy)
    pm = ProjectManager()
    pm.open_board(copy)
    report = pm.close_board()
    assert report is not None and report.source_unchanged is True
    assert pm.close_board() is None

    pm.open_board(copy)
    copy.write_text(copy.read_text() + "\n", encoding="utf-8")  # "KiCad saved it"
    report = pm.close_board()
    assert report is not None and report.source_unchanged is False

    pm.open_board(copy)
    copy.unlink()
    report = pm.close_board()
    assert report is not None and report.source_unchanged is None


def test_workspace_key_is_stable_and_safe(tmp_path: Path) -> None:
    p = tmp_path / "My Board (v2).kicad_pcb"
    key = workspace_key(p)
    assert key == workspace_key(p)
    assert key.startswith(os.path.normcase("My_Board_v2_-"))
    same_file_other_case = Path(str(p).upper())
    if os.path.normcase("A") == "a":  # Windows: case-insensitive filesystem
        assert workspace_key(same_file_other_case) == key
    else:  # Linux: different case means a different file
        assert workspace_key(same_file_other_case) != key
    ws = workspace_for(p, tmp_path / "ws")
    assert ws.root.parent == tmp_path / "ws"
    assert ws.metadata_file.name == "workspace.json"
