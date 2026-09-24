"""Router benchmark: CPU A* vs array wavefront (NumPy, and the GPU when present).

Usage: python tools/bench_router.py [BOARD.kicad_pcb [NET ...]]

Default board: tests/fixtures/boards/router_dense.kicad_pcb. Reports board size,
grid resolution, cells, occupancy build, per-net search time and route quality for
each backend, and a board-routing run. GPU columns say NOT AVAILABLE unless CuPy
(NVIDIA) or dpnp (Intel) initialises on this machine. Numbers are hardware
dependent and are measured, never assumed.
"""

from __future__ import annotations

import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from pcbrouter.compute.detection import detect_gpu
from pcbrouter.compute.gpu_backend import GPUBackend
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, make_plan
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.router import Router
from pcbrouter.routing.search.wavefront import wavefront_search
from pcbrouter.routing.working_board import WorkingBoard

DEFAULT = Path(__file__).resolve().parents[1] / "tests/fixtures/boards/router_dense.kicad_pcb"


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else DEFAULT
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    engine = wb.engine
    box = wb.board.bounds
    assert box is not None
    cell = mm_to_internal(0.1)
    cells = (box.width // cell + 1) * (box.height // cell + 1)
    gpu = GPUBackend(detect_gpu())
    gpu_ok = gpu.available
    print(
        f"machine: {platform.machine()} {platform.processor()} · Python {platform.python_version()}"
    )
    print(
        f"GPU: {gpu.device_info().name} — {'READY' if gpu_ok else 'NOT AVAILABLE'} "
        f"({gpu.detection.status.description})"
    )
    print(
        f"board: {path.name}  {box.width / 1e6:.1f} x {box.height / 1e6:.1f} mm, grid 0.1 mm, "
        f"{cells:,} cells/layer, {len(engine.geometry.copper_layers)} copper layers"
    )
    t0 = time.perf_counter()
    for layer in engine.geometry.copper_layers:
        engine.occupancy(layer, None, mm_to_internal(0.25), cell)
    print(f"occupancy build (all layers, CPU NumPy): {(time.perf_counter() - t0) * 1e3:.1f} ms")
    nets = argv[1:] or [t.net for t in make_plan(wb, BoardRouterSettings()).tasks][:4]
    backends = [
        ("cpu-astar", None),
        ("cpu-wavefront(numpy)", lambda p, **kw: wavefront_search(p, xp=np, **kw)),
    ]
    if gpu_ok:
        backends.append(("gpu-wavefront", lambda p, **kw: wavefront_search(p, xp=gpu.xp, **kw)))
    print(f"{'net':<10}{'backend':<24}{'status':<10}{'time s':>8}{'length mm':>11}{'vias':>6}")
    for net in nets:
        base = None
        for name, fn in backends:
            t0 = time.perf_counter()
            r = Router(engine, search_fn=fn).route_net(RouteRequest(net, candidates=1))
            dt = time.perf_counter() - t0
            best = r.best
            length = best.score.length_nm / 1e6 if best else float("nan")
            print(
                f"{net:<10}{name:<24}{r.status.value:<10}{dt:>8.2f}{length:>11.2f}"
                f"{best.score.vias if best else '-':>6}"
                + (f"   speedup x{base / dt:.2f}" if base and dt > 0 else "")
            )
            base = base or dt
    t0 = time.perf_counter()
    res = BoardRouter(wb, BoardRouterSettings()).run()
    print(f"board routing (CPU): {res.summary()} [{time.perf_counter() - t0:.1f} s]")
    if not gpu_ok:
        print("GPU benchmark: NOT RUN (no usable GPU backend on this machine)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
