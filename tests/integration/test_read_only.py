"""Acceptance: opening, inspecting and closing a board never modifies the file."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from pcbrouter.app.application import main
from pcbrouter.commands import (
    BoardSummaryCommand,
    CloseBoardCommand,
    CommandBus,
    CommandContext,
    OpenBoardCommand,
    ValidateAICommand,
)
from pcbrouter.history import HistoryManager
from pcbrouter.project import ProjectManager

ALL_READABLE = [
    "empty.kicad_pcb",
    "two_components.kicad_pcb",
    "traces.kicad_pcb",
    "vias.kicad_pcb",
    "four_layer.kicad_pcb",
    "kicad5_legacy.kicad_pcb",
    "partially_broken.kicad_pcb",
]


def fingerprint(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns


@pytest.fixture
def board_copy(tmp_path: Path, fixture_path: Callable[[str], Path]) -> Callable[[str], Path]:
    def copy(name: str) -> Path:
        d = tmp_path / "project"
        d.mkdir(exist_ok=True)
        target = d / name
        shutil.copy2(fixture_path(name), target)
        os.utime(target, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        return target

    return copy


@pytest.mark.parametrize("name", ALL_READABLE)
def test_open_inspect_close_leaves_file_identical(
    board_copy: Callable[[str], Path], name: str
) -> None:
    path = board_copy(name)
    before = fingerprint(path)
    dir_before = sorted(p.name for p in path.parent.iterdir())

    bus = CommandBus(CommandContext(project=ProjectManager(), history=HistoryManager()))
    assert bus.dispatch(OpenBoardCommand(path)).success
    assert bus.dispatch(BoardSummaryCommand()).success
    bus.dispatch(ValidateAICommand('{"operation": "route_board"}'))
    closed = bus.dispatch(CloseBoardCommand())
    assert closed.success and closed.data.source_unchanged is True

    assert fingerprint(path) == before  # SHA-256, size and mtime all unchanged
    assert sorted(p.name for p in path.parent.iterdir()) == dir_before  # no side files


def test_cli_inspect_is_read_only(
    board_copy: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    path = board_copy("four_layer.kicad_pcb")
    before = fingerprint(path)
    assert main([str(path), "--inspect", "--no-log-file"]) == 0
    assert fingerprint(path) == before
    assert before[0] in capsys.readouterr().out
