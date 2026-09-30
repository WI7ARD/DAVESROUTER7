"""Dependency-safe partial acceptance of board-routing results (1.1.1, P0-1).

Scenario (single copper layer, so nothing can hop over anything):
    X runs straight across the board; Y must cross X's straight route vertically.
    X is routed and accepted first. Routing Y then needs a rip-up: X's route is
    removed, Y routed, X rerouted around Y's pad.

Accepting only Y must never leave "Y routed, X's route removed, X not re-added".
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbrouter.commands import CommandBus, CommandContext
from pcbrouter.commands.route_commands import AcceptBoardRoutingCommand, RouteBoardCommand
from pcbrouter.domain.via import Via
from pcbrouter.history import HistoryManager
from pcbrouter.project.manager import ProjectManager
from pcbrouter.routing.board_router import (
    BoardRouterSettings,
    BoardRoutingResult,
    copper_dependencies,
)
from pcbrouter.routing.connectivity import NetStatus, net_connectivity
from tests.fixtures.benchmark_suite import _board_text, _project, header


def write_board(tmp_path: Path, walls: bool = False) -> Path:
    h = 12 if walls else 20
    y_top, y_bot = (3.0, 9.0) if walls else (6.0, 17.0)
    # X's pads sit so close to the board edges that nothing passes around them
    x1, x2 = header("J1", 1.2, h / 2, 1, 1), header("J2", 38.8, h / 2, 1, 1)
    y1, y2 = header("J3", 20, y_top, 1, 1), header("J4", 20, y_bot, 1, 1)
    x1.pads[0].net = x2.pads[0].net = "X"
    y1.pads[0].net = y2.pads[0].net = "Y"
    # walls: keepouts above and below the corridor, so X cannot detour
    keep = [(8.0, 0.0, 32.0, 2.0), (8.0, 10.0, 32.0, 12.0)] if walls else []
    text = _board_text("dep", ["F.Cu"], 40, h, [x1, x2, y1, y2], ["X", "Y"], keep)
    path = tmp_path / "dep.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    path.with_suffix(".kicad_pro").write_text(_project(0.25, 0.2, 0.6, 0.3), encoding="utf-8")
    return path


class Session:
    def __init__(self, tmp_path: Path, walls: bool = False) -> None:
        self.project = ProjectManager(workspace_base=tmp_path / "ws")
        self.project.open_board(write_board(tmp_path, walls))
        self.history = HistoryManager()
        self.bus = CommandBus(CommandContext(project=self.project, history=self.history),
                              read_only=True)  # fmt: skip

    @property
    def working(self):  # type: ignore[no-untyped-def]
        assert self.project.working is not None
        return self.project.working

    def connected(self, net: str) -> bool:
        return net_connectivity(self.working.engine.geometry, net).status is (
            NetStatus.FULLY_CONNECTED
        )

    def route(self, nets: list[str], **kw: object) -> BoardRoutingResult:
        r = self.bus.dispatch(RouteBoardCommand(settings=BoardRouterSettings(**kw), nets=nets))
        assert r.success, r.message
        return r.data  # type: ignore[no-any-return]

    def accept(self, result: BoardRoutingResult, nets: set[str] | None) -> object:
        return self.bus.dispatch(AcceptBoardRoutingCommand(result, nets))


def x_then_y(tmp_path: Path, walls: bool = False) -> tuple[Session, BoardRoutingResult]:
    s = Session(tmp_path, walls)
    first = s.route(["X"], allow_ripup=False)
    assert s.accept(first, None).success and s.connected("X")  # type: ignore[attr-defined]
    second = s.route(["Y"], allow_ripup=True, ripup_user_accepted=True)
    return s, second


def test_scenario_really_needs_a_ripup(tmp_path: Path) -> None:
    _s, res = x_then_y(tmp_path)
    assert res.metrics.ripups >= 1, res.log
    assert res.removed_ids and set(res.removed_nets.values()) == {"X"}
    assert res.dependencies.get("Y") == ("X",)
    tracks, _vias, removed = res.objects_for(None)
    assert {t.net_name for t in tracks} == {"X", "Y"} and removed


def test_accepting_only_y_also_accepts_the_moved_x(tmp_path: Path) -> None:
    s, res = x_then_y(tmp_path)
    assert res.required_extra_nets({"Y"}) == {"X"}
    r = s.accept(res, {"Y"})
    assert r.success, r.message  # type: ignore[attr-defined]
    assert "also accepted X" in r.message  # type: ignore[attr-defined]
    assert s.connected("X") and s.connected("Y")
    assert not s.working.engine.run_drc().errors


def test_accepting_only_x_applies_its_whole_change_set(tmp_path: Path) -> None:
    s, res = x_then_y(tmp_path)
    tracks, _v, removed = res.objects_for({"X"})
    assert {t.net_name for t in tracks} == {"X"} and removed  # old X out, new X in
    r = s.accept(res, {"X"})
    assert r.success, r.message  # type: ignore[attr-defined]
    assert s.connected("X") and not s.connected("Y")
    assert not s.working.engine.run_drc().errors


def test_accepting_everything(tmp_path: Path) -> None:
    s, res = x_then_y(tmp_path)
    assert s.accept(res, {"X", "Y"}).success  # type: ignore[attr-defined]
    assert s.connected("X") and s.connected("Y")


def test_a_removal_without_its_replacement_is_refused_atomically(tmp_path: Path) -> None:
    """The old bug, reproduced by attributing X's removal to Y: the command's
    connectivity invariant must refuse it and leave the board untouched."""
    s, res = x_then_y(tmp_path)
    broken = replace(res, removed_nets={i: "Y" for i in res.removed_ids}, dependencies={})
    tracks, _v, removed = broken.objects_for({"Y"})
    assert removed and {t.net_name for t in tracks} == {"Y"}  # X's removal, no new X
    before = s.working.board.fingerprint
    r = s.accept(broken, {"Y"})
    assert not r.success and "disconnect X" in r.message  # type: ignore[attr-defined]
    assert s.working.board.fingerprint == before and s.connected("X")


def test_ripup_never_leaves_a_displaced_net_disconnected(tmp_path: Path) -> None:
    """With walls X has no detour: ripping it for Y would leave X open, so the
    rip-up is rolled back and Y is reported unrouted instead."""
    s, res = x_then_y(tmp_path, walls=True)
    assert not res.removed_ids and not res.dependencies
    assert any("would disconnect X" in line for line in res.log), res.log
    base_x = {t.id for t in res.base_board.tracks if t.net_name == "X"}
    assert base_x and base_x <= {t.id for t in res.final_board.tracks}  # X untouched
    assert s.connected("X")


def test_dependency_closure_is_transitive(tmp_path: Path) -> None:
    _s, res = x_then_y(tmp_path)
    chained = replace(res, dependencies={"A": ("B",), "B": ("C",), "C": ("A",), "Y": ("X",)})
    assert chained.dependency_closure({"A"}) == {"A", "B", "C"}
    assert chained.dependency_closure({"Y"}) == {"X", "Y"}
    assert chained.required_extra_nets({"B"}) == {"A", "C"}


def test_a_via_on_removed_copper_is_a_dependency(tmp_path: Path) -> None:
    _s, res = x_then_y(tmp_path)
    base = res.base_board
    old = next(t for t in base.tracks if t.net_name == "X")
    mid = type(old.start)((old.start.x + old.end.x) // 2, (old.start.y + old.end.y) // 2)
    via = Via("v-test", mid, 600_000, 300_000, "Y", "F.Cu", "F.Cu")
    deps = copper_dependencies(base, (), (via,), {old.id: "X"})
    assert deps == {"Y": ("X",)}


@pytest.mark.parametrize("nets", [{"Y"}, {"X"}, {"X", "Y"}, None])
def test_no_selection_ever_opens_a_net(tmp_path: Path, nets: set[str] | None) -> None:
    s, res = x_then_y(tmp_path)
    r = s.accept(res, nets)
    assert r.success, r.message  # type: ignore[attr-defined]
    assert s.connected("X")
