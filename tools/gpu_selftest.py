"""GPU self-test: run the router's GPU path on *this* machine and report honestly.

Usage (from the repository root):

    python tools/gpu_selftest.py            # writes gpu_selftest_result.json

What it does:
1. Detects GPUs and tries to initialise the GPU backend (CuPy for NVIDIA, dpnp for
   Intel such as Iris Xe / Arc), including a small self-test kernel on the device.
2. Routes fixture nets with three backends — CPU A* (reference), CPU wavefront
   (NumPy) and GPU wavefront — and checks every route with the exact validator.
3. Times the raw wavefront search on progressively finer grids, where a GPU
   should help most.

Nothing is uploaded anywhere. Paste the printed summary (or the JSON file) back
to the developer. It contains hardware names and timings only — no board data.
"""

from __future__ import annotations

import json
import platform
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from pcbrouter import __version__  # noqa: E402
from pcbrouter.compute.detection import detect_gpu  # noqa: E402
from pcbrouter.compute.gpu_backend import GPUBackend  # noqa: E402
from pcbrouter.compute.probe import SKIPPED, gpu_gate  # noqa: E402
from pcbrouter.domain.units import mm_to_internal  # noqa: E402
from pcbrouter.kicad.loader import load_board  # noqa: E402
from pcbrouter.kicad.rule_adapter import load_project_rules  # noqa: E402
from pcbrouter.routing.request import RouteRequest  # noqa: E402
from pcbrouter.routing.router import Router  # noqa: E402
from pcbrouter.routing.search.wavefront import wavefront_search  # noqa: E402
from pcbrouter.routing.working_board import WorkingBoard  # noqa: E402

BOARDS = ROOT / "tests" / "fixtures" / "boards"
CASES = [("router_basic", ["A", "B", "C", "E"]), ("router_dense", ["S3", "S0", "VBUS"])]
GRIDS_MM = (0.1, 0.05, 0.025)


def working(name: str) -> WorkingBoard:
    path = BOARDS / f"{name}.kicad_pcb"
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def main() -> int:
    report: dict[str, Any] = {
        "app_version": __version__,
        "python": platform.python_version(),
        "os": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
    }
    # Hardware gate first: no GPU device -> the self-test is SKIPPED (exit 0), not failed.
    gate = gpu_gate("gpu-selftest")
    report["gpu_probe"] = {"status": gate.status, "library": gate.library,
                           "devices": list(gate.devices), "reason": gate.reason,
                           "checks": list(gate.checks)}  # fmt: skip
    print(gate.summary())
    for check in gate.checks:
        print(f"  probe: {check}")
    if not gate.available:
        report["status"] = SKIPPED
        report["verdict"] = f"GPU self-test SKIPPED: {gate.reason}"
        out = Path("gpu_selftest_result.json")
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n{report['verdict']}\nWrote {out.resolve()}")
        return 0
    report["status"] = "RAN"
    det = detect_gpu()
    report["gpu_detection"] = {
        "status": det.status.value,
        "description": det.status.description,
        "devices": [d.name for d in det.devices],
        "packages": list(det.installed_packages),
    }
    print(f"AI PCB Router {__version__} GPU self-test on {report['os']}")
    print(f"GPU detection: {det.status.description}; devices: {report['gpu_detection']['devices']}")
    gpu = GPUBackend(det)
    try:
        gpu.initialize()
        report["gpu_backend"] = {
            "ready": True,
            "device": gpu.device_info().name,
            "library": det.array_module,
        }
        print(f"GPU backend: READY on {gpu.device_info().name} ({det.array_module})")
    except Exception as exc:
        report["gpu_backend"] = {"ready": False, "error": str(exc)}
        print(f"GPU backend: NOT AVAILABLE — {exc}")

    backends: list[tuple[str, Any]] = [
        ("cpu-astar", None),
        ("cpu-wavefront", lambda p, **kw: wavefront_search(p, xp=np, **kw)),
    ]
    if gpu.initialized:
        backends.append(("gpu-wavefront", lambda p, **kw: wavefront_search(p, xp=gpu.xp, **kw)))

    routes: list[dict[str, Any]] = []
    print(
        f"\n{'board/net':<18}{'backend':<16}{'status':<10}{'legal':<7}"
        f"{'time s':>8}{'len mm':>9}{'vias':>6}"
    )
    for board, nets in CASES:
        wb = working(board)
        engine = wb.engine
        for net in nets:
            for name, fn in backends:
                t0 = time.perf_counter()
                try:
                    r = Router(engine, search_fn=fn).route_net(RouteRequest(net, candidates=1))
                    dt = time.perf_counter() - t0
                    legal = bool(r.best and engine.validator.validate_route(r.best.proposal).legal)
                    row = {
                        "board": board,
                        "net": net,
                        "backend": name,
                        "status": r.status.value,
                        "legal": legal,
                        "seconds": round(dt, 3),
                        "length_mm": round(r.best.score.length_nm / 1e6, 3) if r.best else None,
                        "vias": r.best.score.vias if r.best else None,
                    }
                except Exception as exc:
                    dt = time.perf_counter() - t0
                    row = {
                        "board": board,
                        "net": net,
                        "backend": name,
                        "status": "ERROR",
                        "legal": False,
                        "seconds": round(dt, 3),
                        "error": repr(exc),
                        "trace": traceback.format_exc(limit=3),
                    }
                routes.append(row)
                print(
                    f"{board + '/' + net:<18}{name:<16}{row['status']:<10}{row['legal']!s:<7}"
                    f"{row['seconds']:>8.2f}{row.get('length_mm') or 0:>9.2f}"
                    f"{row.get('vias') or 0:>6}"
                )
    report["routes"] = routes

    # Scaling: the raw search on finer grids (where parallel hardware should help).
    scaling: list[dict[str, Any]] = []
    wb = working("router_dense")
    print("\nwavefront scaling (router_dense S3):")
    for grid in GRIDS_MM:
        for name, fn in backends[1:]:
            req = RouteRequest("S3", candidates=1, grid_resolution=mm_to_internal(grid))
            t0 = time.perf_counter()
            try:
                r = Router(wb.engine, search_fn=fn).route_net(req)
                status = r.status.value
            except Exception as exc:
                status = f"ERROR {exc!r}"
            dt = time.perf_counter() - t0
            scaling.append(
                {"grid_mm": grid, "backend": name, "status": status, "seconds": round(dt, 3)}
            )
            print(f"  grid {grid:>6} mm  {name:<16}{status:<10}{dt:>8.2f} s")
    report["scaling"] = scaling

    ok_gpu = [r for r in routes if r["backend"] == "gpu-wavefront"]
    report["verdict"] = (
        "GPU not available on this machine"
        if not ok_gpu
        else (
            "all GPU routes legal"
            if all(r["legal"] for r in ok_gpu if r["status"] == "SUCCESS")
            and all(r["status"] != "ERROR" for r in ok_gpu)
            else "GPU problems found (see routes)"
        )
    )
    out = Path("gpu_selftest_result.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nVerdict: {report['verdict']}\nWrote {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
