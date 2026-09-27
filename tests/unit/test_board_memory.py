"""Phase 2 board memory: notes, approved decisions, persistence, context."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pcbrouter.ai.memory import BoardMemory, MemoryKind
from pcbrouter.ai.service import AIService
from pcbrouter.ai.session import AIRuntimeConfig, AISession
from pcbrouter.kicad.loader import load_board

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def board():
    return load_board(BOARDS / "can_node.kicad_pcb").board


def test_add_validates_and_retires() -> None:
    mem = BoardMemory("fp")
    entry = mem.add(MemoryKind.NOTE, "  Route  CAN   first ")
    assert entry.text == "Route CAN first" and entry.kind is MemoryKind.NOTE
    with pytest.raises(ValueError):
        mem.add(MemoryKind.NOTE, "   ")
    with pytest.raises(ValueError):
        mem.add(MemoryKind.NOTE, "x" * 501)
    assert mem.retire(entry.id) is True
    assert mem.retire(entry.id) is False
    assert mem.entries == []


def test_render_is_newest_first_and_bounded() -> None:
    mem = BoardMemory("fp")
    for i in range(15):
        mem.add(MemoryKind.NOTE, f"note {i}")
    lines = mem.render()
    assert len(lines) == 10
    assert lines[0].endswith("note 14")
    assert all(line.startswith("MEMORY_NOTE: ") for line in lines)


def test_save_load_round_trip_and_fingerprint_mismatch(tmp_path: Path) -> None:
    mem = BoardMemory.load(tmp_path, "fp1")
    assert mem.entries == [] and mem.directory == tmp_path
    mem.add(MemoryKind.PREFERENCE, "minimize vias")
    mem.add(MemoryKind.DECISION, "approved route", source="approval:abc")
    again = BoardMemory.load(tmp_path, "fp1")
    assert [(e.kind, e.text, e.source) for e in again.entries] == [
        (MemoryKind.PREFERENCE, "minimize vias", "user"),
        (MemoryKind.DECISION, "approved route", "approval:abc"),
    ]
    assert BoardMemory.load(tmp_path, "other-fingerprint").entries == []
    (tmp_path / "ai_memory.json").write_text("not json", encoding="utf-8")
    assert BoardMemory.load(tmp_path, "fp1").entries == []


def test_session_state_lines_include_memory() -> None:
    session = AISession(board(), session_id="s")
    session.memory.add(MemoryKind.NOTE, "GND is sensitive")
    lines = session.session_state_lines()
    assert any(line.startswith("MEMORY_NOTE: ") and "GND" in line for line in lines)


def test_approval_is_captured_as_decision() -> None:
    session = AISession(board(), session_id="s")
    proposal = SimpleNamespace(
        proposal_id="p1",
        current=SimpleNamespace(
            operation=SimpleNamespace(label="Route net"),
            targets=[SimpleNamespace(model_dump=lambda **k: {"type": "net", "name": "GND"})],
        ),
    )
    session._remember_approval(proposal)  # type: ignore[arg-type]
    assert len(session.memory.entries) == 1
    entry = session.memory.entries[0]
    assert entry.kind is MemoryKind.DECISION and "GND" in entry.text
    assert session.memory.entries[0].source == "approval:p1"


def test_service_session_persists_memory(tmp_path: Path) -> None:
    svc = AIService()
    first = svc.start_session(board(), "s1", AIRuntimeConfig(), memory_dir=tmp_path / "ws")
    first.memory.add(MemoryKind.NOTE, "remember this")
    second = svc.start_session(board(), "s2", AIRuntimeConfig(), memory_dir=tmp_path / "ws")
    assert [e.text for e in second.memory.entries] == ["remember this"]
    svc.shutdown()
