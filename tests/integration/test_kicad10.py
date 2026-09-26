"""KiCad 10 boards (format 20260206): load, route, export and Freerouting.

KiCad 10 references nets by name only (``(net "GND")``, no net-code table) and
adds optional IPC-4761 via fields. The fixture is generated from the MIT
``router_basic`` board in exactly that shape (checked against a real KiCad 10
file); it is not a KiCad-made file."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.kicad.writer import ExportError, detect_style, export_board
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, BoardStatus
from pcbrouter.routing.working_board import Provenance, WorkingBoard
from tests.integration.test_freerouting import BOARDS, fake_tools  # noqa: F401 (fixture)

KICAD10_VIA = """\t(via
\t\t(at 28 18)
\t\t(size 0.8)
\t\t(drill 0.4)
\t\t(layers "F.Cu" "B.Cu")
\t\t(capping no)
\t\t(covering
\t\t\t(front no)
\t\t\t(back no)
\t\t)
\t\t(plugging
\t\t\t(front no)
\t\t\t(back no)
\t\t)
\t\t(filling no)
\t\t(net "G")
\t\t(uuid "5a3e0000-0000-4000-8000-0000000000aa")
\t)
"""


def to_kicad10(text: str, with_via: bool = False) -> str:
    codes = {
        m.group(1): m.group(2) for m in re.finditer(r'^\t\(net (\d+) "([^"]*)"\)$', text, re.M)
    }
    text = re.sub(r'^\t\(net \d+ "[^"]*"\)\r?\n', "", text, flags=re.M)  # no net table
    text = re.sub(r'\(net \d+ ("[^"]*")\)', r"(net \1)", text)
    text = re.sub(r"\(net (\d+)\)", lambda m: f'(net "{codes[m.group(1)]}")', text)
    text = re.sub(r"\(version \d+\)", "(version 20260206)", text, count=1)
    text = text.replace('(generator_version "8.0")', '(generator_version "10.0")')
    if with_via:
        cut = text.rstrip().rfind(")")
        text = text[:cut] + KICAD10_VIA + text[cut:]
    return text


def kicad10_copy(tmp_path: Path, with_via: bool = False) -> Path:
    src = BOARDS / "router_basic.kicad_pcb"
    out = tmp_path / "k10.kicad_pcb"
    out.write_text(to_kicad10(src.read_text(encoding="utf-8"), with_via), encoding="utf-8")
    (tmp_path / "k10.kicad_pro").write_bytes(src.with_suffix(".kicad_pro").read_bytes())
    return out


def test_fixture_matches_the_kicad10_shape(tmp_path: Path) -> None:
    text = kicad10_copy(tmp_path).read_text(encoding="utf-8")
    assert "(version 20260206)" in text
    assert re.search(r"\(net \d+", text) is None and '(net "A")' in text
    style = detect_style(text)
    assert not style.net_by_code and style.id_token == "uuid"


def test_load_route_and_export(tmp_path: Path) -> None:
    src = kicad10_copy(tmp_path, with_via=True)
    before = src.read_bytes()
    load = load_board(src)
    assert not [w for w in load.warnings if "newer than" in w.message]
    assert len(load.board.vias) == 1 and load.board.vias[0].net_name == "G"
    assert {n.name for n in load.board.nets} >= {"A", "B", "C", "D", "E", "G"}
    wb = WorkingBoard(load.board, load_project_rules(src))
    result = BoardRouter(wb, BoardRouterSettings()).run()
    assert result.status is BoardStatus.FULLY_ROUTED
    tracks, vias, removed = result.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "route", Provenance.ROUTER_GENERATED)
    out = tmp_path / "k10_routed.kicad_pcb"
    report = export_board(src, hashlib.sha256(before).hexdigest(), wb.source, wb.board, out)
    assert report.ok, report.summary()
    text = out.read_text(encoding="utf-8")
    assert re.search(r"\(net \d+", text) is None  # stays in KiCad 10 net-by-name style
    assert KICAD10_VIA in text  # existing objects preserved byte for byte
    routed = load_board(out).board
    assert len(routed.tracks) == len(load.board.tracks) + len(tracks)
    assert src.read_bytes() == before


def test_newer_formats_are_still_refused() -> None:
    text = to_kicad10((BOARDS / "router_basic.kicad_pcb").read_text(encoding="utf-8"))
    with pytest.raises(ExportError, match="outside the supported export range"):
        detect_style(text.replace("(version 20260206)", "(version 20270101)"))


def test_freerouting_on_a_kicad10_board(tmp_path: Path, fake_tools: None) -> None:  # noqa: F811
    from pcbrouter.app.application import main

    src = kicad10_copy(tmp_path)
    before = src.read_bytes()
    out = tmp_path / "k10_fr.kicad_pcb"
    assert main(["--freeroute", str(src), "--output", str(out), "--passes", "5"]) == 0
    assert src.read_bytes() == before
    text = out.read_text(encoding="utf-8")
    assert "(version 20260206)" in text and re.search(r"\(net \d+", text) is None
    assert len(load_board(out).board.tracks) > 0
