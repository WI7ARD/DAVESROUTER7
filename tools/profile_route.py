"""Where does routing time go? cProfile a whole-board route and group the time.

    python tools/profile_route.py BOARD.kicad_pcb [--mode speed|accuracy] [--budget S]
                                  [--out profile.json]

Runs BoardRouter in this process on one worker (so every function is visible to
the profiler) with the board's own rules and the chosen preset, then groups
self-time by pipeline stage (grid rasterisation, A* loop, geometry/validation, …)
and prints a table with percentages, plus the router's own phase timers.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: (stage, predicate on "path:function") — first match wins
STAGES: list[tuple[str, tuple[str, ...]]] = [
    ("A* search loop (heap, neighbour expansion, costs)", ("search/astar.py:_search",)),
    ("coarse-to-fine helpers (coarse grid, corridor, reachability)",
     ("search/astar.py:_coarse", "search/astar.py:_block", "search/astar.py:_reachable",
      "search/astar.py:search", "search/astar.py:merge_boxes")),
    ("heuristic / other A* setup", ("search/astar.py",)),
    ("obstacle rasterisation (occupancy, distance fields, polygons)",
     ("routing/occupancy.py", "geometry/raster.py")),
    ("grid compile (passable, near, via map, cells_within)", ("search/grid.py",)),
    ("congestion maps", ("routing/congestion.py",)),
    ("path geometry (simplify, snap, collinear)", ("routing/simplify", "routing/geometry",
                                                   "routing/router.py:_geometry")),
    ("exact validation (segments, vias, route)", ("validator", "collision", "geometry/kernel",
                                                  "geometry/predicates", "rules/")),
    ("spatial index queries", ("spatial", "index")),
    ("router orchestration (groups, sources, targets, costs)", ("routing/router.py",)),
    ("board router (passes, commits, rip-up)", ("routing/board_router.py",
                                                "routing/working_board.py")),
    ("planning / escape analysis / connectivity", ("routing/escape.py", "routing/connectivity",
                                                   "routing/planner")),
    ("NumPy internals", ("numpy",)),
    ("Python builtins (heapq, dict, list, …)", ("{built-in", "~:")),
]  # fmt: skip


def stage_of(key: str) -> str:
    for name, needles in STAGES:
        if any(n in key for n in needles):
            return name
    return "other"


def profile_board(board: Path, mode: str, budget: float) -> dict[str, Any]:
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import WorkingBoard

    t0 = time.perf_counter()
    wb = WorkingBoard(load_board(board).board, load_project_rules(board))
    load_s = time.perf_counter() - t0
    base = RouteRequest("", candidates=1)
    settings = adjust_board_settings(BoardRouterSettings(base_request=base, parallel_workers=0),
                                     base, RouteMode(mode))  # fmt: skip
    settings = replace(settings, budget_s=budget)
    prof = cProfile.Profile()
    t0 = time.perf_counter()
    prof.enable()
    result = BoardRouter(wb, settings).run()
    prof.disable()
    wall = time.perf_counter() - t0
    stats = pstats.Stats(prof)
    groups: dict[str, float] = {}
    top: list[tuple[float, str]] = []
    for (path, _line, func), (_cc, _nc, tt, _ct, _callers) in stats.stats.items():  # type: ignore[attr-defined]
        key = f"{path.replace(chr(92), '/')}:{func}"
        groups[stage_of(key)] = groups.get(stage_of(key), 0.0) + tt
        top.append((tt, f"{Path(path).name}:{func}"))
    total = sum(groups.values()) or 1.0
    top.sort(reverse=True)
    m = result.metrics
    return {
        "board": board.name,
        "mode": mode,
        "result": result.summary(),
        "load_s": round(load_s, 2),
        "route_wall_s": round(wall, 1),
        "profiled_s": round(total, 1),
        "stages_pct": {
            k: round(100 * v / total, 1) for k, v in sorted(groups.items(), key=lambda kv: -kv[1])
        },
        "router_phase_s": {
            "grid": round(m.grid_s, 1),
            "search": round(m.search_s, 1),
            "geometry": round(m.geometry_s, 1),
            "validate": round(m.validate_s, 1),
        },
        "top_functions": [(f, round(t, 2)) for t, f in top[:15]],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("boards", nargs="+", type=Path)
    ap.add_argument("--mode", choices=["speed", "accuracy"], default="accuracy")
    ap.add_argument("--budget", type=float, default=300.0)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    reports = []
    for board in args.boards:
        rep = profile_board(board, args.mode, args.budget)
        reports.append(rep)
        print(f"\n## {rep['board']} ({rep['mode']}): {rep['result']}")
        print(f"route {rep['route_wall_s']} s under the profiler; phases {rep['router_phase_s']}")
        for name, pct in rep["stages_pct"].items():
            print(f"  {pct:5.1f} %  {name}")
    if args.out:
        args.out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
