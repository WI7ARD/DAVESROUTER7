"""CPU A* vs the fused relaxation kernel on a SYCL device, same boards, same rules.

    python tools/bench_gpu.py BOARD.kicad_pcb [...] [--device gpu|opencl:cpu|level_zero:gpu]
                              [--mode speed|accuracy] [--budget S] [--report gpu_report.json]

On an Intel Iris Xe / Arc machine run it with the default ``--device gpu``. Each
board is routed twice on one worker — CPU A* (the product default) and GPU mode
(every search on the device kernel) — and both results are validated. Recorded:
initialisation, board preparation, search time, total route time, nets, vias,
length, internal-check errors and fallbacks. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def route(board: Path, mode: str, budget: float, factory: Any) -> dict[str, Any]:
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import Provenance, WorkingBoard

    t0 = time.perf_counter()
    wb = WorkingBoard(load_board(board).board, load_project_rules(board))
    prep = time.perf_counter() - t0
    base = RouteRequest("", candidates=1)
    st = adjust_board_settings(BoardRouterSettings(base_request=base, parallel_workers=0),
                               base, RouteMode(mode))  # fmt: skip
    t0 = time.perf_counter()
    res = BoardRouter(wb, replace(st, budget_s=budget), router_factory=factory).run()
    total = time.perf_counter() - t0
    tracks, vias, removed = res.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "bench", Provenance.ROUTER_GENERATED)
    m = res.metrics
    return {
        "prepare_s": round(prep, 2), "route_s": round(total, 1), "search_s": round(m.search_s, 1),
        "grid_s": round(m.grid_s, 1), "nets": f"{m.nets_completed}/{m.nets_attempted}",
        "vias": m.new_vias, "length_mm": round(m.total_length_nm / 1e6, 1),
        "check_errors": len(wb.engine.run_drc().errors), "summary": res.summary(),
    }  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("boards", nargs="+", type=Path)
    ap.add_argument("--device", default="gpu")
    ap.add_argument("--mode", choices=["speed", "accuracy"], default="accuracy")
    ap.add_argument("--budget", type=float, default=600.0)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv)

    from pcbrouter.compute.probe import gpu_gate
    from pcbrouter.compute.sycl_check import run_diagnostic
    from pcbrouter.routing.backend import HybridSearch, SearchMode
    from pcbrouter.routing.router import Router

    if args.device == "gpu":
        gate = gpu_gate("bench-gpu")  # hardware gate: no device, no GPU work
        if not gate.available:
            print(f"GPU {gate.status}: {gate.reason}")
    t0 = time.perf_counter()
    diag = run_diagnostic(args.device, bench=True)
    init_s = time.perf_counter() - t0
    print(diag.verdict())
    report: dict[str, Any] = {"machine": platform.platform(), "processor": platform.processor(),
                              "device": args.device, "diagnostic": diag.to_dict(),
                              "init_s": round(init_s, 2), "boards": []}  # fmt: skip
    if not diag.compute_verified:
        print("The device did not pass the diagnostic: only the CPU can be benchmarked.")
    else:
        from pcbrouter.compute.sycl_check import device_backend

        gpu = device_backend(args.device)
    for board in args.boards:
        row: dict[str, Any] = {"board": board.name, "cpu": route(board, args.mode, args.budget,
                                                                 None)}  # fmt: skip
        print(f"{board.name} CPU A*: {row['cpu']['summary']}")
        if diag.compute_verified:
            hybrid = HybridSearch(SearchMode.GPU, gpu)
            row["gpu"] = route(board, args.mode, args.budget,
                               lambda e, h=hybrid: Router(e, search_fn=h))  # fmt: skip
            row["gpu"]["searches"] = dict(hybrid.used)
            row["gpu"]["fallback_reasons"] = hybrid.fallback_reasons[:5]
            print(f"{board.name} device: {row['gpu']['summary']} searches={hybrid.used}")
        report["boards"].append(row)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
