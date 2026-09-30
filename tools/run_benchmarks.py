"""Benchmark suite runner: routes every suite board through the real command line
(``pcbrouter --route``) and writes BENCHMARKS.md-style tables plus JSON.

    python tools/run_benchmarks.py --out bench_out                 # source checkout
    python tools/run_benchmarks.py --exe "C:/.../pcbrouter.exe"    # installed app
    options: --modes speed accuracy  --workers 0 3  --budget 300  --boards tiny ...

Each run records wall time, CPU time and peak memory of the routing process (where
the OS reports them), completion, vias, routed length, export status and whether
the exported file passed the internal checks. The impossible board must end with
a clear failure (exit code != 0 and no hang) — that is checked, not assumed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def _child_usage() -> tuple[float, float]:
    try:
        import resource

        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        return ru.ru_utime + ru.ru_stime, ru.ru_maxrss / 1024.0
    except ImportError:  # Windows
        return 0.0, 0.0


def run_one(exe: list[str], board: Path, mode: str, workers: int, budget: float,
            out_dir: Path) -> dict[str, Any]:  # fmt: skip
    tag = f"{board.stem}_{mode}_w{workers}"
    report = out_dir / f"{tag}.json"
    output = out_dir / f"{tag}.kicad_pcb"
    cmd = [*exe, str(board), "--route", "--mode", mode, "--workers", str(workers),
           "--budget", str(budget), "--output", str(output), "--report", str(report)]  # fmt: skip
    cpu0, _ = _child_usage()
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=budget + 300)
        code, tail = proc.returncode, (proc.stdout + proc.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        code, tail = -9, "HUNG: killed after budget + 300 s"
    wall = time.monotonic() - t0
    cpu1, rss = _child_usage()
    data: dict[str, Any] = {}
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8"))
    m = data.get("metrics", {})
    reopened = None
    if output.exists():
        try:
            from pcbrouter.kicad.loader import load_board

            ob = load_board(output).board
            reopened = {"tracks": len(ob.tracks), "vias": len(ob.vias)}
        except Exception as exc:  # recorded: a benchmark must show a broken file
            reopened = {"error": str(exc)}
    return {
        "board": board.stem, "mode": mode, "workers_requested": workers,
        "workers": data.get("workers"), "exit_code": code, "wall_s": round(wall, 1),
        "route_s": data.get("route_s"), "cpu_s": round(cpu1 - cpu0, 1) or None,
        "peak_rss_mb": round(rss) or None,
        "nets": f"{m.get('nets_completed', '?')}/{m.get('nets_attempted', '?')}",
        "vias": m.get("new_vias"), "length_mm": m.get("total_routed_length_mm"),
        "parallel_batches": m.get("parallel_batches"),
        "parallel_conflicts": m.get("parallel_conflicts"),
        "verified": data.get("verified"), "export": data.get("export"),
        "reopened": reopened, "message": data.get("message"), "tail": tail[-400:],
    }  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    from tests.fixtures.benchmark_suite import ORDER, write_suite

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("bench_out"))
    ap.add_argument("--exe", default=None, help="installed pcbrouter executable")
    ap.add_argument("--modes", nargs="+", default=["speed", "accuracy"])
    ap.add_argument("--workers", nargs="+", type=int, default=[0, -1])
    ap.add_argument("--budget", type=float, default=300.0)
    ap.add_argument("--boards", nargs="+", default=ORDER)
    ap.add_argument("--extra", nargs="*", type=Path, default=[], help="more .kicad_pcb files")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    suite = write_suite(args.out / "boards")
    boards = [suite[b] for b in args.boards] + list(args.extra)
    exe = [args.exe] if args.exe else [sys.executable, "-c",
                                       "import sys; from pcbrouter.app.application import main; "
                                       "sys.exit(main(sys.argv[1:]))"]  # fmt: skip
    rows: list[dict[str, Any]] = []
    for board in boards:
        for mode in args.modes:
            for w in args.workers:
                row = run_one(exe, board, mode, w, args.budget, args.out)
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("board", "mode", "workers", "exit_code",
                                                      "wall_s", "nets", "vias", "verified")}),
                      flush=True)  # fmt: skip
    (args.out / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    lines = ["| Board | Mode | Workers | Nets routed | Vias | Length (mm) | Route s | Wall s | "
             "CPU s | Peak MB | Exit | Export verified |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for r in rows:
        lines.append(
            f"| {r['board']} | {r['mode']} | {r['workers']} | {r['nets']} | {r['vias']} | "
            f"{r['length_mm']} | {r['route_s']} | {r['wall_s']} | {r['cpu_s']} | "
            f"{r['peak_rss_mb']} | {r['exit_code']} | {r['verified']} |"
        )
    (args.out / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    impossible = [r for r in rows if r["board"] == "impossible"]
    hung = [r for r in rows if r["exit_code"] == -9]
    bad = [r for r in impossible if r["exit_code"] == 0]
    if hung or bad:
        print(f"FAIL: hung={len(hung)} impossible-reported-success={len(bad)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
