"""Acceptance suite for the user's own boards (benchmarks/boards/acceptance.json).

For every board and mode: route in an isolated worker with a hard timeout
(the Real100 harness worker), export the routed board next to the run, then
judge it with real KiCad DRC (the oracle; KiCad 8 and, when the wrappers
exist, KiCad 9) against the unrouted source. Writes ``acceptance.json`` and
``acceptance.md`` in the output folder.

Usage: python tools/acceptance_suite.py --out DIR [--ids esc,reva] [--modes speed,accuracy]
       [--budget-scale 1.0]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcbrouter.benchmark.real100 import oracle_rows  # noqa: E402
from pcbrouter.kicad.oracle import find_oracle  # noqa: E402

MANIFEST = ROOT / "benchmarks" / "boards" / "acceptance.json"
GRACE_S = 180.0  # load + export + DRC of the routed board, outside the routing budget


def stage(spec: dict[str, Any], dest: Path) -> Path:
    """Copy the board with the project it must be judged under (never in place)."""
    src = MANIFEST.parent / spec["board"]
    dest.mkdir(parents=True, exist_ok=True)
    board = dest / src.name
    shutil.copy2(src, board)
    for suffix in (".kicad_pro", ".kicad_dru"):
        side = src.with_suffix(suffix)
        if side.is_file():
            shutil.copy2(side, board.with_suffix(suffix))
    if spec.get("project"):
        shutil.copy2(MANIFEST.parent / spec["project"], board.with_suffix(".kicad_pro"))
    return board


def route(board: Path, mode: str, budget: float, prefix: Path) -> dict[str, Any]:
    cmd = [
        sys.executable, "-m", "pcbrouter.benchmark.real100", "worker",
        "--board", str(board), "--mode", mode, "--budget", str(budget),
        "--check-validity", "--save-routed", str(prefix),
        "--experience", str(prefix.parent / "exp"),
    ]  # fmt: skip
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=budget + GRACE_S, check=False,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "wall_s": round(time.perf_counter() - t0, 1)}
    wall = round(time.perf_counter() - t0, 1)
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"status": "worker_error", "wall_s": wall, "error": proc.stderr[-2000:]}
    return {**payload, "wall_s": wall}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ids", default=None)
    ap.add_argument("--modes", default="speed,accuracy")
    ap.add_argument("--budget-scale", type=float, default=1.0)
    args = ap.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = set(args.ids.split(",")) if args.ids else None
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for spec in manifest["boards"]:
        if ids and spec["id"] not in ids:
            continue
        board = stage(spec, out / spec["id"] / "source")
        for mode in args.modes.split(","):
            budget = float(spec["budget_s"][mode]) * args.budget_scale
            print(f"route {spec['id']} {mode} budget {budget:.0f} s", flush=True)
            res = route(board, mode, budget, out / spec["id"] / f"routed_{mode}")
            rows.append({"id": spec["id"], "name": spec["name"], "mode": mode,
                         "budget_s": budget, **res})  # fmt: skip
    judged: dict[str, list[dict[str, Any]]] = {}
    for label, cli, py in (("kicad8", None, None), ("kicad9", "kicad9-cli", "kicad9-python")):
        tool = find_oracle(cli, py)
        if tool is None:
            continue
        judged[label] = oracle_rows(
            rows, tool, ("refilled", "as_exported"), out / f"oracle_{label}"
        )
    for r in rows:
        r["oracle"] = {
            label: next(
                (j["oracle"] for j in js if j["id"] == r["id"] and j["mode"] == r["mode"]), None
            )
            for label, js in judged.items()
        }
    (out / "acceptance.json").write_text(json.dumps(rows, indent=1, default=str) + "\n")

    lines = ["# Acceptance suite", "", "| Board | Mode | Nets | Status | Wall s | Peak RSS MB | "
             "Failures | KiCad 8 | KiCad 9 |", "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    def verdict(o: dict[str, Any], label: str) -> str:
        v = o.get(label)
        if not v:
            return "-"
        var = (v.get("variants") or {}).get(v.get("verdict_variant") or "", {})
        extra = f" gen_err={var.get('new_generated_errors')}" if var else ""
        return f"{v.get('status')}{extra}"

    for r in rows:
        m = r.get("metrics") or {}
        o = r.get("oracle") or {}

        lines.append(
            f"| {r['id']} | {r['mode']} | {m.get('nets_completed')}/{m.get('nets_attempted')} | "
            f"{r.get('route_status', r.get('status'))} | {r.get('wall_s')} | "
            f"{r.get('peak_rss_mb', '-')} | {r.get('failure_reasons', {})} | "
            f"{verdict(o, 'kicad8')} | {verdict(o, 'kicad9')} |"
        )
    lines += ["", "## Failed nets", ""]
    for r in rows:
        for ex in r.get("failed_examples") or []:
            lines.append(f"- {r['id']} {r['mode']}: {ex}")
    (out / "acceptance.md").write_text("\n".join(lines) + "\n")
    print(out / "acceptance.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
