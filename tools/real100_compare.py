"""Compare Real100 runs: totals, per-board deltas, regressions and failure reasons.

Two ways to use it:

**Compare existing runs**::

    python tools/real100_compare.py BASELINE.jsonl [BASELINE2.jsonl ...] \\
        --candidate CAND.jsonl [CAND2.jsonl ...] [--fail-on-regression]
    python tools/real100_compare.py RUN.jsonl [...]        # summary of one run set

A board/mode pair counts as a **regression** when the candidate completes fewer
nets, fails where the baseline worked (timeout / worker error), or needs more than
25 % extra vias for the same completion. Wall-time changes are listed but never
counted: they depend on the machine and on what else was running.

**Paired A/B of two routing strategies** (runs the benchmarks itself)::

    python tools/real100_compare.py --baseline fixed --candidate policy_v2.json \\
        --repeat 3 [--ids K003,K022] [--modes speed,accuracy] [--guard] \\
        [--check-validity] [--json ab.json] [--profiles]

Baseline and candidate run side by side (same boards, same load), ``--repeat``
times; each board gets one paired difference per repeat. ``--guard`` also runs
the guard boards (small_2layer, medium_4layer, dense_2layer). The verdict uses
the acceptance gate in ``pcbrouter.benchmark.ab`` and prints ACCEPTED,
EXPERIMENTAL or REJECTED. ``--json`` writes everything, including (with
``--profiles``) each board's profile so the trainer can use the results as
board-level evidence (``tools/train_policy.py --evidence``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pcbrouter.benchmark import ab

Key = tuple[str, str]
GUARD_BOARDS = ("small_2layer", "medium_4layer", "dense_2layer")


def load(paths: list[Path]) -> dict[Key, dict[str, Any]]:
    rows: dict[Key, dict[str, Any]] = {}
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[(r["id"], r["mode"])] = r
    return rows


def nets(r: dict[str, Any]) -> tuple[int, int]:
    m = r.get("metrics") or {}
    if r.get("status") != "ok":
        return 0, 0
    return int(m.get("nets_completed", 0)), int(m.get("nets_attempted", 0))


def outcome(r: dict[str, Any]) -> str:
    if r.get("status") != "ok":
        return str(r.get("status"))
    return str(r.get("route_status"))


def summary(rows: dict[Key, dict[str, Any]], title: str) -> None:
    done = sum(nets(r)[0] for r in rows.values())
    tried = sum(nets(r)[1] for r in rows.values())
    by = Counter(outcome(r) for r in rows.values())
    reasons: Counter[str] = Counter()
    for r in rows.values():
        reasons.update(r.get("failure_reasons") or {})
    print(f"{title}: {len(rows)} runs, nets {done}/{tried}, {dict(sorted(by.items()))}")
    if reasons:
        top = ", ".join(f"{k} {v}" for k, v in reasons.most_common())
        print(f"  unrouted nets by reason: {top}")


def compare(base: dict[Key, dict[str, Any]], cand: dict[Key, dict[str, Any]]) -> int:
    regressions = 0
    lines: list[str] = []
    for key in sorted(set(base) & set(cand)):
        b, c = base[key], cand[key]
        (bd, bt), (cd, ct) = nets(b), nets(c)
        bv = (b.get("metrics") or {}).get("new_vias") or 0
        cv = (c.get("metrics") or {}).get("new_vias") or 0
        worse = cd < bd or (outcome(b) == "FULLY_ROUTED" and outcome(c) != "FULLY_ROUTED")
        worse |= b.get("status") == "ok" and c.get("status") != "ok"
        worse |= cd == bd and bd > 0 and cv > bv * 1.25 + 2
        better = cd > bd or (b.get("status") != "ok" and c.get("status") == "ok")
        if worse or better or outcome(b) != outcome(c):
            tag = "REGRESSION" if worse else ("better" if better else "changed")
            regressions += worse
            lines.append(
                f"  {tag:<10} {key[0]} {key[1]:<8} {outcome(b)} {bd}/{bt} v{bv} "
                f"-> {outcome(c)} {cd}/{ct} v{cv} "
                f"({b.get('wall_s', 0):.0f}s -> {c.get('wall_s', 0):.0f}s)"
            )
    missing = sorted(set(base) ^ set(cand))
    print("\n".join(lines) or "  no per-board changes")
    if missing:
        print(f"  not in both runs: {', '.join(f'{i}/{m}' for i, m in missing)}")
    print(f"regressions: {regressions}")
    return regressions


# ------------------------------------------------------------------ A/B mode
def policy_identity(spec: str) -> dict[str, Any]:
    """What exactly a strategy spec is: fixed, random:SEED, or a policy file
    (with its content id and file hash)."""
    if spec in ("fixed", "") or spec.startswith("random"):
        return {"spec": spec}
    from pcbrouter.learning.policy import policy_from_spec

    path = Path(spec)
    pol = policy_from_spec(spec)
    pid = getattr(pol, "policy_id", None)
    trained = json.loads(path.read_text(encoding="utf-8")).get("trained") or {}
    return {
        "spec": str(path),
        "policy_id": pid() if callable(pid) else None,
        "dataset_id": trained.get("dataset_id"),
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "selective": hasattr(pol, "select"),
    }


def _launch_real100(spec: str, mode: str, a: argparse.Namespace) -> subprocess.Popen[str]:
    cmd = [
        sys.executable, str(ROOT / "tools" / "benchmark_real100.py"), "run",
        "--profile", a.profile, "--modes", mode, "--no-experience", "--policy", spec,
    ]  # fmt: skip
    if a.ids:
        cmd += ["--ids", a.ids]
    if a.check_validity:
        cmd.append("--check-validity")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _launch_guard(spec: str, mode: str, out: Path) -> subprocess.Popen[str]:
    cmd = [
        sys.executable, str(ROOT / "tools" / "run_benchmarks.py"), "--out", str(out),
        "--modes", mode, "--workers", "0", "--boards", *GUARD_BOARDS,
    ]  # fmt: skip
    if spec != "fixed":
        cmd += ["--policy", spec]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _wait_path(proc: subprocess.Popen[str]) -> Path:
    out, _ = proc.communicate()
    last = [x for x in out.strip().splitlines() if x.strip()]
    if proc.returncode != 0 or not last or not last[-1].endswith(".jsonl"):
        raise RuntimeError(f"benchmark run failed ({proc.returncode}): {out[-800:]}")
    return Path(last[-1].strip())


def board_profiles(ids: set[str], workdir: Path) -> dict[str, dict[str, float]]:
    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.learning.features import board_profile
    from pcbrouter.routing.board_router import BoardRouterSettings, make_plan
    from pcbrouter.routing.request import RouteRequest
    from pcbrouter.routing.working_board import WorkingBoard

    out: dict[str, dict[str, float]] = {}
    for bid in sorted(ids):
        boards = sorted((workdir / "prepared" / bid).glob("*.kicad_pcb"))
        if not boards:
            continue
        wb = WorkingBoard(load_board(boards[0]).board, load_project_rules(boards[0]))
        plan = make_plan(wb, BoardRouterSettings(base_request=RouteRequest("")))
        out[bid] = board_profile(wb.board, plan.tasks)
    return out


def run_ab(a: argparse.Namespace) -> int:
    base_spec, cand_spec = a.baseline, a.candidate[0]
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    stamp = time.strftime("%Y%m%dT%H%M%S")
    outdir = Path(a.workdir) / "ab" / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    ident = {"baseline": policy_identity(base_spec), "candidate": policy_identity(cand_spec)}
    print(f"A/B {base_spec} vs {cand_spec}, {a.repeat} repeat(s), modes {modes}", flush=True)
    base_runs: list[dict[Key, dict[str, Any]]] = []
    cand_runs: list[dict[Key, dict[str, Any]]] = []
    gbase: list[dict[Key, dict[str, Any]]] = []
    gcand: list[dict[Key, dict[str, Any]]] = []
    runs: list[dict[str, Any]] = []
    for rep in range(a.repeat):
        # every strategy x mode side by side: paired runs share the machine load
        procs = {
            (arm, m): _launch_real100(spec, m, a)
            for arm, spec in (("base", base_spec), ("cand", cand_spec))
            for m in modes
        }
        paths = {k: _wait_path(p) for k, p in procs.items()}
        runs.append({f"{k[0]}_{k[1]}": str(v) for k, v in paths.items()})
        base_runs.append(load([paths[("base", m)] for m in modes]))
        cand_runs.append(load([paths[("cand", m)] for m in modes]))
        print(f"  repeat {rep + 1}: real100 done", flush=True)
        if a.guard:
            gp = {}
            for arm, spec in (("base", base_spec), ("cand", cand_spec)):
                for m in modes:
                    gdir = outdir / f"guard_{arm}_{m}_r{rep}"
                    gp[(arm, m)] = (_launch_guard(spec, m, gdir), gdir)
            rows: dict[str, dict[Key, dict[str, Any]]] = {"base": {}, "cand": {}}
            for (arm, _m), (proc, out) in gp.items():
                proc.communicate()
                res = out / "results.json"
                if res.exists():
                    data = json.loads(res.read_text(encoding="utf-8"))
                    rows[arm].update(ab.guard_rows(data if isinstance(data, list) else []))
            gbase.append(rows["base"])
            gcand.append(rows["cand"])
            print(f"  repeat {rep + 1}: guard boards done", flush=True)
    pairs = ab.pair_runs(base_runs, cand_runs)
    gpairs = ab.pair_runs(gbase, gcand) if a.guard else []
    verdict = ab.gate(pairs, gpairs, a.repeat)
    for line in ab.format_pairs(pairs, "Real100"):
        print(line)
    if gpairs:
        for line in ab.format_pairs(gpairs, "guard boards"):
            print(line)
    agg = verdict["aggregate"]
    print(
        f"aggregate: nets {agg['base_nets_mean']} -> {agg['cand_nets_mean']} "
        f"({agg['net_delta']:+}), gained {agg['nets_gained']}, lost {agg['nets_lost']}; "
        f"boards improved {agg['improved']}, unchanged {agg['unchanged']}, "
        f"regressed {agg['regressed']}, noisy {agg['noisy']}; sign test p={agg['sign_test_p']}"
    )
    print(
        f"  vias {agg['vias_delta']:+}, wall {agg['wall_delta_s']:+} s, failures "
        f"{agg['base_failures']} -> {agg['cand_failures']}, new-copper DRC errors "
        f"{agg['base_drc_errors']} -> {agg['cand_drc_errors']}, decisions {agg['decisions']}"
    )
    if verdict["guard"]:
        g = verdict["guard"]
        print(
            f"guard: nets {g['base_nets_mean']} -> {g['cand_nets_mean']}, "
            f"regressed {g['regressed']}, noisy {g['noisy']}"
        )
    for name, ok in verdict["checks"].items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"VERDICT: {verdict['status']}")
    report = {
        "schema": "pcbrouter-ab/1",
        "created": stamp,
        "identity": ident,
        "repeat": a.repeat,
        "modes": modes,
        "profile": a.profile,
        "runs": runs,
        "boards": [p.to_json() for p in pairs],
        "guard_boards": [p.to_json() for p in gpairs],
        "gate": verdict,
    }
    if a.profiles:
        prof = board_profiles({p.key[0] for p in pairs}, Path(a.workdir))
        for b in report["boards"]:
            b["profile"] = prof.get(b["id"])
    target = a.json or outdir / "ab.json"
    Path(target).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"report: {target}")
    return 1 if (a.fail_on_regression and verdict["status"] == "REJECTED") else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("files", nargs="*", type=Path, help="baseline run .jsonl files")
    ap.add_argument("--candidate", nargs="+")
    ap.add_argument("--fail-on-regression", action="store_true")
    ap.add_argument("--baseline", default=None, help="A/B mode: fixed | random:SEED | policy.json")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--ids", default=None)
    ap.add_argument("--modes", default="speed,accuracy")
    ap.add_argument("--profile", default="standard")
    ap.add_argument("--guard", action="store_true", help="also A/B the guard boards")
    ap.add_argument("--check-validity", action="store_true")
    ap.add_argument("--profiles", action="store_true", help="store board profiles in --json")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--workdir", default=str(ROOT / "benchmarks" / "real100" / "work"))
    a = ap.parse_args(argv)
    if a.baseline is not None:
        if not a.candidate or len(a.candidate) != 1:
            ap.error("A/B mode needs exactly one --candidate strategy")
        return run_ab(a)
    if not a.files:
        ap.error("give run files, or --baseline/--candidate strategies")
    base = load(a.files)
    summary(base, "baseline" if a.candidate else "run")
    if not a.candidate:
        return 0
    cand = load([Path(x) for x in a.candidate])
    summary(cand, "candidate")
    n = compare(base, cand)
    return 1 if (n and a.fail_on_regression) else 0


if __name__ == "__main__":
    sys.exit(main())
