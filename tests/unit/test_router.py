"""Stage 4 CPU autorouter: legality, vias, limits, determinism, commit and undo."""

from __future__ import annotations

import hashlib
import math
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


def test_conservative_unknown_minimum_fails_fast_with_guidance() -> None:
    """Power boards without rules: refuse in milliseconds, not after minutes
    of searching paths the validator must reject anyway."""
    import time

    wb = WorkingBoard(load_board(BOARDS / "router_basic.kicad_pcb").board)
    t0 = time.perf_counter()
    res = route(wb.engine, "B", preferred_width=MM(0.25))
    dt = time.perf_counter() - t0
    assert res.status is RouteStatus.RULE_UNKNOWN and res.reason is FailureReason.RULE_UNKNOWN
    assert "net classes" in res.message
    assert dt < 5, f"rule refusal took {dt:.1f}s"


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


def test_shared_grid_cache_skips_occupancy_rebuild(basic: WorkingBoard) -> None:
    """R2: a second identical search reuses compiled inputs (no occupancy work)."""
    from pcbrouter.routing.search.grid import compile_grid

    engine = basic.engine
    calls = 0
    real_occupancy = engine.occupancy

    def counting(*args: object, **kw: object) -> object:
        nonlocal calls
        calls += 1
        return real_occupancy(*args, **kw)  # type: ignore[arg-type]

    engine.occupancy = counting  # type: ignore[method-assign]
    try:
        cache: dict = {}
        key = ("net-A-key",)
        first = compile_grid(
            engine,
            "A",
            ("F.Cu", "B.Cu"),
            MM(0.25),
            MM(0.6),
            MM(0.1),
            None,
            cache=cache,
            cache_key=key,
        )
        assert calls > 0
        before = calls
        engine._occupancy.clear()
        second = compile_grid(
            engine,
            "A",
            ("F.Cu", "B.Cu"),
            MM(0.25),
            MM(0.6),
            MM(0.1),
            None,
            cache=cache,
            cache_key=key,
        )
        assert calls == before, "cached compile must not touch occupancy"
        assert [c.tobytes() for c in second.passable] == [c.tobytes() for c in first.passable]
        assert len(cache) == 1
        # a different window compiles separately
        from pcbrouter.domain.geometry import BoundingBox

        other_key = ("net-A-other-window",)
        compile_grid(
            engine,
            "A",
            ("F.Cu", "B.Cu"),
            MM(0.25),
            MM(0.6),
            MM(0.1),
            BoundingBox(0, 0, 1_000_000, 1_000_000),
            cache=cache,
            cache_key=other_key,
        )
        assert len(cache) == 2
    finally:
        engine.occupancy = real_occupancy  # type: ignore[method-assign]


def test_cached_grid_is_isolated_from_search_mutations(basic: WorkingBoard) -> None:
    """R2: per-search repairs/penalties must never corrupt the shared inputs."""
    import numpy as np

    from pcbrouter.routing.search.grid import compile_grid

    engine = basic.engine
    cache: dict = {}
    key = ("net-A-iso",)
    first = compile_grid(
        engine,
        "A",
        ("F.Cu", "B.Cu"),
        MM(0.25),
        MM(0.6),
        MM(0.1),
        None,
        cache=cache,
        cache_key=key,
    )
    cells = np.nonzero(first.passable[0].reshape(-1))[0][:10]
    first.block(0, cells)
    assert not first.passable[0].reshape(-1)[cells].any()
    second = compile_grid(
        engine,
        "A",
        ("F.Cu", "B.Cu"),
        MM(0.25),
        MM(0.6),
        MM(0.1),
        None,
        cache=cache,
        cache_key=key,
    )
    assert second.passable[0].reshape(-1)[cells].all()


def test_threaded_grid_build_matches_serial(monkeypatch: object) -> None:
    """R5: parallel layer builds assemble byte-identical grids."""
    import pcbrouter.routing.search.grid as grid_module
    from pcbrouter.routing.search.grid import compile_grid

    wb = working("router_dense.kicad_pcb")
    kwargs: dict = {
        "engine": wb.engine,
        "net": "USB_N",
        "layers": ("F.Cu", "B.Cu"),
        "width": MM(0.25),
        "via_diameter": MM(0.6),
        "cell": MM(0.1),
    }
    monkeypatch.setattr(grid_module, "MAX_GRID_WORKERS", 1)  # type: ignore[attr-defined]
    serial = compile_grid(**kwargs)  # type: ignore[arg-type]
    wb2 = working("router_dense.kicad_pcb")  # cold caches: real concurrent build
    kwargs["engine"] = wb2.engine
    monkeypatch.setattr(grid_module, "MAX_GRID_WORKERS", 4)  # type: ignore[attr-defined]
    threaded = compile_grid(**kwargs)  # type: ignore[arg-type]
    assert threaded.spec == serial.spec
    assert threaded.notes == serial.notes
    assert threaded.rules_complete == serial.rules_complete
    for a, b in zip(threaded.passable, serial.passable, strict=True):
        assert a.tobytes() == b.tobytes()
    assert (threaded.via_ok is None) == (serial.via_ok is None)
    if threaded.via_ok is not None and serial.via_ok is not None:
        assert threaded.via_ok.tobytes() == serial.via_ok.tobytes()


def test_board_router_shares_one_grid_cache(basic: WorkingBoard) -> None:
    """R2: every Router in a board job uses the same bounded grid cache."""
    from pcbrouter.routing.board_router import BoardRouter

    made: list[Router] = []

    def factory(engine: BoardEngine) -> Router:
        router = Router(engine)
        made.append(router)
        return router

    job = BoardRouter(basic, router_factory=factory)
    job.run()
    assert len(made) >= 1
    caches = {id(r.grid_cache) for r in made}
    assert len(caches) == 1
    shared = made[0].grid_cache
    assert shared is job._grid_cache
    assert 0 < len(shared) <= BoardRouter.GRID_CACHE_SIZE + 2


def test_heuristic_weight_below_one_is_rejected(basic: WorkingBoard) -> None:
    """R3: weights below 1 would be inadmissible in the wrong direction."""
    res = route(basic.engine, "A", heuristic_weight=0.5)
    assert res.status is RouteStatus.INVALID_REQUEST, res.summary()


def test_weighted_search_is_faster_and_stays_legal() -> None:
    """R3: weight 1.5 collapses expansions with a bounded length cost."""
    engine = working("router_dense.kicad_pcb").engine
    base = route(engine, "S3", candidates=1)
    assert base.status is RouteStatus.SUCCESS, base.summary()
    fast = route(engine, "S3", candidates=1, heuristic_weight=1.5)
    assert fast.status is RouteStatus.SUCCESS, fast.summary()
    assert fast.metrics.expanded_nodes <= base.metrics.expanded_nodes
    assert fast.best is not None and base.best is not None
    assert_octilinear_and_legal(engine, fast.best)
    assert fast.best.score.length_nm <= 1.5 * base.best.score.length_nm
    assert fast.metrics.search_s <= base.metrics.search_s + 0.5


def test_weighted_search_is_deterministic() -> None:
    """R3: same weight + seed → identical grid path."""
    engine = working("router_dense.kicad_pcb").engine
    first = route(engine, "S3", candidates=1, heuristic_weight=1.5)
    second = route(engine, "S3", candidates=1, heuristic_weight=1.5)
    assert first.status is second.status is RouteStatus.SUCCESS
    assert first.best is not None and second.best is not None
    assert first.best.proposal.segments == second.best.proposal.segments


def _brute_force_optimal_cost(problem: object) -> float | None:
    """Independent Dijkstra over the same state space WITHOUT dominance pruning.

    Mirrors the move set and edge costs of astar.search exactly (same operand
    order), so any cost difference proves the slack pruning changed the answer.
    Returns None when no target is reachable.
    """
    import heapq
    import math

    from pcbrouter.routing.search.astar import DIRS, NO_DIR, SQRT2

    g = problem.grid  # type: ignore[attr-defined]
    nx, n, nl = g.nx, g.n, len(g.layers)
    ny = g.ny
    cell = float(g.spec.cell)
    cm = problem.cost  # type: ignore[attr-defined]
    passable = [p.reshape(-1).tobytes() for p in g.passable]
    near = [p.reshape(-1).tobytes() for p in g.near]
    target = [t.reshape(-1).tobytes() for t in problem.targets]  # type: ignore[attr-defined]
    penalty = [None if p is None else p.reshape(-1) for p in g.penalty]
    factor = [None if f is None else f.reshape(-1) for f in g.factor]
    vias_on = problem.vias_enabled and g.via_ok is not None and nl > 1  # type: ignore[attr-defined]
    via_ok = g.via_ok.reshape(-1).tobytes() if vias_on and g.via_ok is not None else b""
    vlim = problem.max_vias  # type: ignore[attr-defined]
    nv = (vlim + 1) if (vias_on and vlim is not None) else 1
    track_vias = vias_on and vlim is not None
    lf = problem.layer_factor  # type: ignore[attr-defined]
    prox = cm.proximity_factor
    dirs = [0, 2, 4, 6] if not problem.octilinear else list(range(8))  # type: ignore[attr-defined]
    bend = (0.0, cm.bend45_nm, cm.bend90_nm)
    heap: list[tuple[float, int]] = []
    best: dict[int, float] = {}
    for li, cells in enumerate(problem.sources):  # type: ignore[attr-defined]
        pl = passable[li]
        for idx in cells.tolist():
            if not pl[idx]:
                continue
            s = (li * n + idx) * 9 + NO_DIR
            if s not in best:
                best[s] = 0.0
                heapq.heappush(heap, (0.0, s))
    while heap:
        gc, s = heapq.heappop(heap)
        if gc > best.get(s, math.inf):
            continue
        d = s % 9
        rest = s // 9
        idx = rest % n
        rest //= n
        li = rest % nl
        v = rest // nl
        if target[li][idx]:
            return gc
        r, c = divmod(idx, nx)
        pl = passable[li]
        nr_l = near[li]
        pen = penalty[li]
        fac = factor[li]
        base = (v * nl + li) * n
        for nd in dirs:
            if d != NO_DIR:
                turn = abs(nd - d)
                if turn > 4:
                    turn = 8 - turn
                if turn > 2:
                    continue
            else:
                turn = 0
            dx, dy = DIRS[nd]
            rr, cc = r + dy, c + dx
            if rr < 0 or rr >= ny or cc < 0 or cc >= nx:
                continue
            ni = rr * nx + cc
            if not pl[ni]:
                continue
            if dx and dy and not (pl[r * nx + cc] and pl[rr * nx + c]):
                continue
            step = cell * (SQRT2 if dx and dy else 1.0)
            w = lf[li] * (float(fac[ni]) if fac is not None else 1.0)
            cost = step * w * (1.0 + prox * nr_l[ni])
            if pen is not None:
                cost += step * float(pen[ni])
            cost += bend[turn]
            ng = gc + cost
            ns = (base + ni) * 9 + nd
            if ng < best.get(ns, math.inf):
                best[ns] = ng
                heapq.heappush(heap, (ng, ns))
        if vias_on and via_ok[idx] and (not track_vias or v + 1 < nv):
            v2 = v + 1 if track_vias else v
            for l2 in range(nl):
                if l2 == li or not passable[l2][idx]:
                    continue
                ns = ((v2 * nl + l2) * n + idx) * 9 + NO_DIR
                ng = gc + problem.via_cost  # type: ignore[attr-defined]
                if ng < best.get(ns, math.inf):
                    best[ns] = ng
                    heapq.heappush(heap, (ng, ns))
    return None


def test_slack_pruning_preserves_optimal_cost() -> None:
    """R4: the cell_best dominance prune never changes the optimal grid cost.

    Real searches are replayed through an independent, pruning-free Dijkstra
    over the same state space; optimal costs must match to rounding (distinct
    optima can differ by 1 ULP under float summation order), proving the prune
    only ever skips losers.
    """
    import numpy as np

    from pcbrouter.routing.search.astar import SearchStatus, search

    dense = working("router_dense.kicad_pcb")

    def penalised(layer: str, spec: object) -> object:

        return np.full((spec.ny, spec.nx), 0.5)  # type: ignore[attr-defined]

    cases = [
        (working("router_basic.kicad_pcb"), "A", None),
        (working("router_basic.kicad_pcb"), "B", None),
        (dense, "USB_N", None),
        (dense, "S3", None),
        (dense, "USB_N", penalised),
    ]
    for wb, net, penalties in cases:
        recorded: list[tuple[object, dict, object]] = []
        real = search

        def spy(
            problem: object, _real: object = real, _rec: list = recorded, **kw: object
        ) -> object:
            outcome = _real(problem, **kw)  # type: ignore[operator]
            _rec.append((problem, kw, outcome))
            return outcome

        router = Router(wb.engine, search_fn=spy)
        if penalties is None:
            res = router.route_net(RouteRequest(net, candidates=1))
        else:
            res = router.route_net(RouteRequest(net, candidates=1), penalties=penalties)  # type: ignore[arg-type]
        assert res.status is RouteStatus.SUCCESS, res.summary()
        assert recorded, "no searches recorded"
        for _problem, _kw, outcome in recorded:
            assert outcome.status is SearchStatus.FOUND  # type: ignore[attr-defined]
            optimal = _brute_force_optimal_cost(_problem)
            assert optimal is not None
            assert math.isclose(optimal, outcome.cost, rel_tol=1e-9), (  # type: ignore[attr-defined]
                f"{net}: pruned={outcome.cost} optimal={optimal}"  # type: ignore[attr-defined]
            )


def test_pour_sources_are_thinned_and_routed(tmp_path: Path) -> None:
    """Power nets: giant pours thin to boundary + samples, still route."""
    import numpy as np

    from pcbrouter.routing.router import SOURCE_CELL_CAP, _thin_sources
    from pcbrouter.routing.search.astar import search

    cells = np.arange(100 * 100, dtype=np.int64)  # solid 100x100 copper block
    thinned = _thin_sources([cells], 100, 100, cap=1000)
    assert len(thinned) == 1
    kept = set(thinned[0].tolist())
    # every boundary cell survives; deep interior is sampled
    edge_cells = list(range(100)) + list(range(9900, 10000)) + [i * 100 for i in range(100)]
    edge_cells += [i * 100 + 99 for i in range(100)]
    for edge in edge_cells:
        assert edge in kept
    assert len(kept) < len(cells)
    again = _thin_sources([cells], 100, 100, cap=1000)
    assert again[0].tolist() == thinned[0].tolist()  # deterministic
    assert len(kept) <= 1000 + 400  # cap plus the always-kept boundary

    path = _pour_board(tmp_path, 2000, box=(14, 0.5, 29.5, 19.5))
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    sizes: list[int] = []
    real = search

    def spy(problem: object, **kw: object) -> object:
        sizes.append(sum(len(s) for s in problem.sources))  # type: ignore[attr-defined]
        return real(problem, **kw)  # type: ignore[arg-type]

    res = Router(wb.engine, search_fn=spy).route_net(
        RouteRequest("C", candidates=1, grid_resolution=MM(0.05))
    )
    assert res.status is RouteStatus.SUCCESS, res.summary()
    assert sizes, "no searches ran"
    assert max(sizes) <= SOURCE_CELL_CAP + 20000
    assert_octilinear_and_legal(wb.engine, res.best)


def test_node_limit_escalates_to_weighted_search() -> None:
    """Power nets: hitting the node cap retries once weighted instead of dying."""
    from pcbrouter.routing.search.astar import search as real_search

    engine = working("router_dense.kicad_pcb").engine
    weights: list[float] = []
    real = real_search

    def spy(problem: object, **kw: object) -> object:
        weights.append(float(kw.get("heuristic_weight", 1.0)))
        return real(problem, **kw)  # type: ignore[operator]

    res = Router(engine, search_fn=spy).route_net(
        RouteRequest("S3", candidates=1, node_limit=20000)
    )
    assert res.status is RouteStatus.SUCCESS, res.summary()
    assert weights[0] == 1.0
    assert 1.5 in weights, "no weighted escalation after the node limit"
    assert any("retried weighted" in d for d in res.details)
    assert_octilinear_and_legal(engine, res.best)


def test_no_escalation_when_already_weighted() -> None:
    """Speed mode stays single-attempt: no redundant weighted retry."""
    from pcbrouter.routing.search.astar import search as real_search

    engine = working("router_dense.kicad_pcb").engine
    calls = 0
    real = real_search

    def spy(problem: object, **kw: object) -> object:
        nonlocal calls
        calls += 1
        return real(problem, **kw)  # type: ignore[operator]

    res = Router(engine, search_fn=spy).route_net(
        RouteRequest("S3", candidates=1, node_limit=20000, heuristic_weight=1.5)
    )
    assert res.status is RouteStatus.SUCCESS, res.summary()
    assert calls == 1
