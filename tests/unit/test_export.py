"""Stage 9: round-trip-safe KiCad export, DRC gate, sessions and crash recovery."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from pcbrouter.commands import (
    CommandBus,
    CommandContext,
    ExportBoardCommand,
    OverwriteSourceCommand,
)
from pcbrouter.commands.route_commands import AcceptRouteCommand, RouteNetCommand
from pcbrouter.history import HistoryManager
from pcbrouter.kicad.kicad_cli import parse_drc_report
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.kicad.writer import (
    ExportStatus,
    compose,
    default_export_path,
    detect_style,
    export_board,
)
from pcbrouter.project.manager import ProjectManager
from pcbrouter.project.session_store import (
    SessionError,
    load_session,
    restore_session,
    save_session,
    session_data,
)
from pcbrouter.routing.connectivity import NetStatus
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, Provenance, WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_board(tmp_path: Path, name: str = "router_basic") -> Path:
    for ext in (".kicad_pcb", ".kicad_pro"):
        src = BOARDS / f"{name}{ext}"
        if src.exists():
            (tmp_path / src.name).write_bytes(src.read_bytes())
    return tmp_path / f"{name}.kicad_pcb"


def routed(path: Path, nets: tuple[str, ...] = ("A", "B")) -> WorkingBoard:
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    for net in nets:
        res = Router(wb.engine).route_net(RouteRequest(net, request_id=f"t-{net}", candidates=1))
        assert res.best is not None, res.summary()
        wb.commit_proposals([res.best.proposal], f"Route {net}", Provenance.USER_ACCEPTED)
    return wb


def signature(board: object) -> tuple[set[object], set[object]]:
    t = {(x.id, x.start, x.end, x.width, x.layer, x.net_name) for x in board.tracks}  # type: ignore[attr-defined]
    v = {(x.id, x.position, x.diameter, x.drill, x.net_name) for x in board.vias}  # type: ignore[attr-defined]
    return t, v


# ------------------------------------------------------------------ writer
def test_unmodified_export_is_byte_identical(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    wb = WorkingBoard(load_board(src).board, load_project_rules(src))
    out = default_export_path(src)
    assert out.name == "router_basic_routed.kicad_pcb"
    rep = export_board(src, sha(src), wb.source, wb.board, out)
    assert rep.ok, rep.summary()
    assert out.read_bytes() == src.read_bytes()
    assert default_export_path(src).name == "router_basic_routed-2.kicad_pcb"


def test_routed_export_reloads_with_same_copper_connectivity_and_drc(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    before = sha(src)
    wb = routed(src)
    out = tmp_path / "router_basic_routed.kicad_pcb"
    rep = export_board(
        src,
        before,
        wb.source,
        wb.board,
        out,
        provenance={k: v.value for k, v in wb.provenance.items()},
    )
    assert rep.ok, rep.summary()
    assert rep.added_tracks > 0 and sha(src) == before  # source untouched
    text = out.read_text(encoding="utf-8")
    assert text.startswith(src.read_text(encoding="utf-8").rstrip()[:-1].rstrip())
    reloaded = load_board(out).board
    assert signature(reloaded) == signature(wb.board)
    assert len(reloaded.components) == len(wb.board.components)
    wb2 = WorkingBoard(reloaded, load_project_rules(src))
    for net in ("A", "B"):
        assert wb2.engine.connectivity.nets[net].status is NetStatus.FULLY_CONNECTED
    assert len(wb2.engine.run_drc().errors) == len(wb.engine.run_drc().errors) == 0
    side = json.loads(rep.sidecar.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert side["source"]["sha256"] == before and side["output_sha256"] == sha(out)
    assert len(side["objects"]) == rep.added_tracks + rep.added_vias
    assert "key" not in json.dumps(side).lower()
    # re-export of the export is a fixed point for the source copper
    wb3 = WorkingBoard(reloaded, load_project_rules(src))
    rep2 = export_board(out, sha(out), wb3.source, wb3.board, tmp_path / "again.kicad_pcb")
    assert rep2.ok and (tmp_path / "again.kicad_pcb").read_bytes() == out.read_bytes()


def test_export_blocks_changed_source_overwrite_and_missing_copper(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    wb = routed(src, ("A",))
    out = tmp_path / "out.kicad_pcb"
    rep = export_board(src, "0" * 64, wb.source, wb.board, out)
    assert rep.status is ExportStatus.EXPORT_BLOCKED_SOURCE_CHANGED and not out.exists()
    rep = export_board(src, sha(src), wb.source, wb.board, src)
    assert rep.status is ExportStatus.EXPORT_BLOCKED_OVERWRITE
    rep = export_board(src, sha(src), wb.source, wb.board, tmp_path / "x.txt")
    assert rep.status is ExportStatus.EXPORT_BLOCKED_OVERWRITE
    # the working board lost source copper → export refuses (it only adds)
    from dataclasses import replace

    stripped = replace(wb.board, tracks=wb.board.tracks[1:])
    if wb.source.tracks:
        rep = export_board(src, sha(src), wb.source, stripped, out)
        assert rep.status is ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT
        assert not out.exists()


def test_explicit_overwrite_makes_a_backup_first(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    original = src.read_bytes()
    wb = routed(src, ("A",))
    rep = export_board(
        src,
        sha(src),
        wb.source,
        wb.board,
        src,
        overwrite_source=True,
        backup_dir=tmp_path / "backups",
    )
    assert rep.ok and rep.backup is not None
    assert rep.backup.read_bytes() == original
    assert signature(load_board(src).board) == signature(wb.board)


def test_unsupported_versions_are_blocked_not_rewritten() -> None:
    for name in ("kicad5_legacy.kicad_pcb", "unsupported_version.kicad_pcb"):
        text = (BOARDS / name).read_text(encoding="utf-8")
        with pytest.raises(Exception) as info:
            detect_style(text)
        assert (
            getattr(info.value, "status", None) is ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT
        )
    with pytest.raises(Exception) as info:
        compose("(not_a_board)", [], [], {})
    assert getattr(info.value, "status", None) is ExportStatus.EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT


def test_output_follows_file_style_crlf_and_kicad6_tokens(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    text = src.read_text(encoding="utf-8")
    crlf = tmp_path / "crlf.kicad_pcb"
    crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    (tmp_path / "crlf.kicad_pro").write_bytes((tmp_path / "router_basic.kicad_pro").read_bytes())
    wb = routed(crlf, ("A",))
    out = tmp_path / "crlf_routed.kicad_pcb"
    assert export_board(crlf, sha(crlf), wb.source, wb.board, out).ok
    data = out.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")  # no bare LF injected
    style = detect_style(text)
    assert style.id_token == "uuid" and style.quoted_ids
    # KiCad 6 style: tstamp ids, unquoted
    k6 = re.sub(r'\(uuid "([^"]+)"\)', r"(tstamp \1)", text).replace(
        "(version 20240108)", "(version 20211014)"
    )
    s6 = detect_style(k6)
    assert s6.id_token == "tstamp" and not s6.quoted_ids


# ------------------------------------------------------------------ commands
def open_bus(tmp_path: Path, src: Path) -> tuple[CommandBus, ProjectManager]:
    project = ProjectManager(workspace_base=tmp_path / "ws")
    project.open_board(src)
    bus = CommandBus(CommandContext(project=project, history=HistoryManager()), read_only=True)
    return bus, project


def test_export_command_drc_gate_and_read_only_overwrite(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    before = sha(src)
    bus, project = open_bus(tmp_path, src)
    r = bus.dispatch(RouteNetCommand(RouteRequest("A", candidates=1)))
    assert bus.dispatch(AcceptRouteCommand(r.data.best)).success
    out = tmp_path / "router_basic_routed.kicad_pcb"
    res = bus.dispatch(ExportBoardCommand(out))
    assert res.success and "Internal checks passed" in res.message, res.message
    assert "manufactur" not in res.message.lower()
    # overwrite is a board-modifying command: blocked by the read-only policy
    blocked = bus.dispatch(OverwriteSourceCommand(src))
    assert not blocked.success and sha(src) == before
    # DRC gate: inject an error (a track shorting two nets) without validation
    wb = project.working
    assert wb is not None
    a_pad = next(p for p in wb.board.pads if p.net_name == "A")
    b_pad = next(p for p in wb.board.pads if p.net_name == "B")
    from pcbrouter.domain.track import Track

    bad = Track("bad-short", a_pad.position, b_pad.position, 250_000, "F.Cu", "A")
    wb.commit_objects([bad], [], (), "inject", Provenance.USER_ACCEPTED, validate=False)
    out2 = tmp_path / "bad.kicad_pcb"
    gated = bus.dispatch(ExportBoardCommand(out2))
    assert not gated.success and "EXPORT_BLOCKED_DRC" in gated.message and not out2.exists()
    forced = bus.dispatch(ExportBoardCommand(out2, allow_unverified=True))
    assert forced.success and "UNVERIFIED" in forced.message
    side = json.loads(out2.with_name(out2.name + ".pcbrouter.json").read_text(encoding="utf-8"))
    assert side["verified"] is False
    project.close_board()
    assert sha(src) == before


def test_kicad_drc_report_parsing_is_labelled() -> None:
    res = parse_drc_report(
        {
            "violations": [
                {"severity": "error", "description": "Clearance"},
                {"severity": "warning", "description": "silk"},
            ],
            "unconnected_items": [{}],
        },
        "8.0.4",
    )
    assert res.ran and not res.passed and res.errors == 1 and res.unconnected == 1
    assert res.summary().startswith("KiCad DRC FAILED")
    assert parse_drc_report({"violations": [], "unconnected_items": []}).passed


# ------------------------------------------------------------------ sessions
def test_session_save_restore_and_recovery_rules(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    wb = routed(src, ("A",))
    wb.set_locked("net:A", True) if hasattr(wb, "set_locked") else None
    wb.net_constraints["A"] = {"width": 300_000}
    path = save_session(tmp_path / "s.json", session_data(wb, src.name, sha(src)))
    data = load_session(path)
    fresh = WorkingBoard(load_board(src).board, load_project_rules(src))
    commit = restore_session(fresh, data, sha(src))
    assert commit is not None and fresh.board.fingerprint == wb.board.fingerprint
    assert fresh.net_constraints == {"A": {"width": 300_000}}
    assert fresh.lock_state() == wb.lock_state()
    fresh.undo()  # restoring is an ordinary undoable commit
    assert fresh.board.fingerprint == fresh.source.fingerprint
    # a session of another board version is never applied
    with pytest.raises(SessionError):
        restore_session(fresh, data, "f" * 64)
    # corrupt / foreign / future files
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(SessionError):
        load_session(tmp_path / "bad.json")
    (tmp_path / "other.json").write_text('{"format": "x"}', encoding="utf-8")
    with pytest.raises(SessionError):
        load_session(tmp_path / "other.json")
    data["format_version"] = 99
    (tmp_path / "new.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SessionError):
        load_session(tmp_path / "new.json")
    # sessions never hold credentials
    with pytest.raises(SessionError):
        save_session(tmp_path / "k.json", {"format": "x", "api_key": "sk-123"})


def test_restoring_illegal_copper_is_rejected(tmp_path: Path) -> None:
    src = copy_board(tmp_path)
    wb = WorkingBoard(load_board(src).board, load_project_rules(src))
    a = next(p for p in wb.board.pads if p.net_name == "A")
    b = next(p for p in wb.board.pads if p.net_name == "B")
    data = session_data(wb, src.name, sha(src))
    data["added_tracks"] = [
        {
            "id": "x",
            "start": [a.position.x, a.position.y],
            "end": [b.position.x, b.position.y],
            "width": 250_000,
            "layer": "F.Cu",
            "net": "A",
        }
    ]
    with pytest.raises(CommitError):  # validated like any other commit
        restore_session(wb, data, sha(src))
    assert wb.board.fingerprint == wb.source.fingerprint
