"""Stage 4 CPU autorouter: legality, vias, limits, determinism, commit and undo."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from pcbrouter.board_engine import BoardEngine
from pcbrouter.commands import AcceptRouteCommand, CommandBus, CommandContext, RouteNetCommand
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.history import HistoryManager
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.project.manager import ProjectManager
from pcbrouter.routing.connectivity import NetStatus, net_connectivity
from pcbrouter.routing.proposal import ProposalSource
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import FailureReason, RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, Provenance, WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"
MM = mm_to_internal


def working(name: str) -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


@pytest.fixture
def basic() -> WorkingBoard:
    return working("router_basic.kicad_pcb")


def route(engine: BoardEngine, net: str, **kw: object) -> object:
    return Router(engine).route_net(RouteRequest(net, request_id=f"t-{net}", **kw))  # type: ignore[arg-type]


def assert_octilinear_and_legal(engine: BoardEngine, cand: object) -> None:
    prop = cand.proposal  # type: ignore[attr-defined]
    assert prop.source is ProposalSource.CPU_ROUTER
    for s in prop.segments:
        dx, dy = s.end.x - s.start.x, s.end.y - s.start.y
        assert dx == 0 or dy == 0 or abs(dx) == abs(dy), s
        res = engine.validator.validate_segment(prop.net, s.layer, s.start, s.end, s.width)
        assert res.legal, res.messages()
    for v in prop.vias:
        res = engine.validator.validate_via(
            prop.net, v.position, v.start_layer, v.end_layer, v.diameter, v.drill
        )
        assert res.legal, res.messages()
    assert engine.validator.validate_route(prop).legal


def test_route_through_vias_under_f_cu_keepout(basic: WorkingBoard) -> None:
    engine = basic.engine
    res = route(engine, "A")
    assert res.status is RouteStatus.SUCCESS, res.summary()
    best = res.best
    assert len(best.proposal.vias) == 2 and {"F.Cu", "B.Cu"} <= set(best.proposal.layers)
    assert all(s.width == MM(0.25) for s in best.proposal.segments)  # net-class width
    for cand in res.candidates:
        assert_octilinear_and_legal(engine, cand)
    assert len(res.candidates) >= 2
    assert len({c.proposal.segments for c in res.candidates}) == len(res.candidates)


def test_max_via_limit_is_enforced_and_explained(basic: WorkingBoard) -> None:
    res = route(basic.engine, "A", max_vias=1)
    assert res.status is RouteStatus.NO_ROUTE
    assert res.reason is FailureReason.VIA_LIMIT and "2 via" in res.message
    assert res.blockers


def test_keepout_detour_and_layer_restriction(basic: WorkingBoard) -> None:
    engine = basic.engine
    res = route(engine, "B")
    assert res.status is RouteStatus.SUCCESS
    assert_octilinear_and_legal(engine, res.best)
    assert res.best.proposal.length > MM(7)  # straight line is 7 mm: it had to detour
    only_f = route(engine, "A", allowed_layers=("F.Cu",))
    assert only_f.status is RouteStatus.NO_ROUTE
    assert only_f.reason is FailureReason.LAYER_RESTRICTION


def test_width_below_minimum_is_invalid(basic: WorkingBoard) -> None:
    res = route(basic.engine, "B", preferred_width=MM(0.1))
    assert res.status is RouteStatus.INVALID_REQUEST and "minimum" in res.message
    wide = route(basic.engine, "B", preferred_width=MM(0.4))
    assert wide.status is RouteStatus.SUCCESS
    assert all(s.width == MM(0.4) for s in wide.best.proposal.segments)


def test_multi_pad_net_and_existing_route(basic: WorkingBoard) -> None:
    res = route(basic.engine, "E")
    assert res.status is RouteStatus.SUCCESS and res.connections_total == 2
    assert res.best.connections == 2
    assert route(basic.engine, "G").status is RouteStatus.ALREADY_CONNECTED
    assert route(basic.engine, "nope").status is RouteStatus.INVALID_REQUEST


def test_determinism(basic: WorkingBoard) -> None:
    a = route(basic.engine, "B")
    b = route(basic.engine, "B")
    assert [c.proposal.segments for c in a.candidates] == [
        c.proposal.segments for c in b.candidates
    ]


def test_limits_and_cancellation(basic: WorkingBoard) -> None:
    res = route(basic.engine, "A", node_limit=50, candidates=1)
    assert res.status is RouteStatus.TIMEOUT and res.reason is FailureReason.TIMEOUT
    stop = threading.Event()
    stop.set()
    res2 = Router(basic.engine).route_net(RouteRequest("A", candidates=1), cancel=stop)
    assert res2.status is RouteStatus.CANCELLED


def test_commit_undo_redo_and_connectivity(basic: WorkingBoard) -> None:
    source = basic.source
    fp0 = basic.fingerprint
    n_tracks = len(source.tracks)
    res = route(basic.engine, "A")
    commit = basic.commit_proposals([res.best.proposal], "Route A", Provenance.USER_ACCEPTED)
    assert len(source.tracks) == n_tracks  # source board untouched
    assert basic.fingerprint != fp0 and basic.modified
    geo = basic.engine.geometry
    assert net_connectivity(geo, "A").status is NetStatus.FULLY_CONNECTED
    assert basic.engine.connectivity.nets["A"].status is NetStatus.FULLY_CONNECTED
    assert all(basic.provenance_of(t.id) is Provenance.USER_ACCEPTED for t in commit.added_tracks)
    # the router now sees the new copper as an obstacle for other nets
    drc = basic.engine.run_drc()
    assert not drc.errors, [v.message for v in drc.errors]
    assert basic.undo() is commit
    assert basic.fingerprint == fp0 and not basic.modified
    assert net_connectivity(basic.engine.geometry, "A").status is NetStatus.UNROUTED
    basic.redo(commit)
    assert basic.board is commit.after


def test_crossing_nets_and_stale_proposal(basic: WorkingBoard) -> None:
    c = route(basic.engine, "C")
    d_before = route(basic.engine, "D")  # computed before C is committed
    basic.commit_proposals([c.best.proposal], "Route C")
    engine = basic.engine
    d = route(engine, "D")
    assert d.status is RouteStatus.SUCCESS
    assert_octilinear_and_legal(engine, d.best)
    straight = {s.layer for s in d_before.best.proposal.segments}
    c_layers = {s.layer for s in c.best.proposal.segments}
    if straight & c_layers and not d_before.best.proposal.vias:
        with pytest.raises(CommitError):
            basic.commit_proposals([d_before.best.proposal], "stale D")


def test_four_layer_board_uses_inner_layers() -> None:
    wb = working("router_4layer.kicad_pcb")
    res = route(wb.engine, "N1")
    assert res.status is RouteStatus.SUCCESS, res.summary()
    assert {"In1.Cu", "In2.Cu"} & set(res.best.proposal.layers)
    assert_octilinear_and_legal(wb.engine, res.best)


def test_commands_through_bus_and_history(tmp_path: Path) -> None:
    src = BOARDS / "router_basic.kicad_pcb"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    project = ProjectManager(workspace_base=tmp_path)
    project.open_board(src)
    history = HistoryManager()
    bus = CommandBus(CommandContext(project=project, history=history), read_only=True)
    r = bus.dispatch(RouteNetCommand(RouteRequest("B", candidates=1)))
    assert r.success and r.data.best is not None
    a = bus.dispatch(AcceptRouteCommand(r.data.best))
    assert a.success and history.can_undo
    assert project.working is not None and project.working.modified
    assert project.engine is not None
    assert project.engine.connectivity.nets["B"].status is NetStatus.FULLY_CONNECTED
    history.undo()
    assert not project.working.modified
    history.redo()
    assert project.working.modified
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    project.close_board()
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_rule_unknown_blocks_routing() -> None:
    # Without the .kicad_pro no rule states a track width: the router refuses
    # instead of inventing one.
    wb = WorkingBoard(load_board(BOARDS / "router_basic.kicad_pcb").board)
    res = route(wb.engine, "B")
    assert res.status is RouteStatus.RULE_UNKNOWN and res.reason is FailureReason.RULE_UNKNOWN
    assert not res.candidates


def _pour_board(
    tmp_path: Path, vertices: int, box: tuple[float, float, float, float] = (14, 16, 17, 19.5)
) -> Path:
    """router_basic + a same-net zone fill for C with many vertices (a GND-like pour
    in a free corner, not touching any pad)."""
    import math

    text = (BOARDS / "router_basic.kicad_pcb").read_text(encoding="utf-8")
    pts = []
    for i in range(vertices):
        t = i / vertices
        # perimeter walk of the box 14..17 x 16..19.5 with a small wave (many vertices)
        x0, y0, x1, y1 = box
        wx, hy = x1 - x0, y1 - y0
        per = 2 * (wx + hy)
        d = t * per
        if d < wx:
            x, y = x0 + d, y0
        elif d < wx + hy:
            x, y = x1, y0 + (d - wx)
        elif d < 2 * wx + hy:
            x, y = x1 - (d - wx - hy), y1
        else:
            x, y = x0, y1 - (d - 2 * wx - hy)
        w = 0.05 * math.sin(40 * math.pi * t)
        pts.append(f"(xy {x + w:.4f} {y + w:.4f})")
    zone = (
        '\t(zone (net 3) (net_name "C") (layer "B.Cu") '
        '(uuid "5a3e0000-0000-4000-8000-0000000009ff") '
        '(name "C_pour")\n\t\t(connect_pads (clearance 0.3))\n\t\t(min_thickness 0.25)\n'
        "\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        "\t\t(polygon (pts (xy 14 16) (xy 17 16) (xy 17 19.5) (xy 14 19.5)))\n"
        f'\t\t(filled_polygon (layer "B.Cu") (pts {" ".join(pts)}))\n\t)\n'
    )
    out = tmp_path / "router_basic.kicad_pcb"
    out.write_text(text.rstrip()[:-1] + zone + ")\n", encoding="utf-8")
    (tmp_path / "router_basic.kicad_pro").write_bytes(
        (BOARDS / "router_basic.kicad_pro").read_bytes()
    )
    return out


def test_big_same_net_pour_does_not_stall_grid_building(tmp_path: Path) -> None:
    """Regression: a large many-vertex pour of the routed net (a GND plane) made grid
    building take minutes: a full-window distance field was computed per polygon
    edge. The pour here covers most of the board with 20k vertices."""
    import time

    path = _pour_board(tmp_path, 20_000, box=(14, 0.5, 29.5, 19.5))
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    pour = next(
        c for c in wb.engine.geometry.copper.values() if c.net == "C" and c.kind.name == "ZONE_FILL"
    )
    assert pour.shapes[0].core.polygon.edge_count >= 19_000
    t0 = time.perf_counter()
    occ = wb.engine.occupancy("B.Cu", "C", mm_to_internal(0.25), mm_to_internal(0.1))
    assert time.perf_counter() - t0 < 10, "rasterising the pour stalled"
    assert occ.counts().get("same net", 0) > 20_000  # the pour really was rasterised
    res = route(wb.engine, "C", candidates=1)
    assert res.status is RouteStatus.SUCCESS, res.summary()
    assert_octilinear_and_legal(wb.engine, res.best)
