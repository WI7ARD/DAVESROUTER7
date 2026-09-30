"""Compare Real100 runs: totals, per-board deltas, regressions and failure reasons.

    python tools/real100_compare.py BASELINE.jsonl [BASELINE2.jsonl ...] \
        --candidate CAND.jsonl [CAND2.jsonl ...] [--fail-on-regression]
    python tools/real100_compare.py RUN.jsonl [...]        # summary of one run set

A board/mode pair counts as a **regression** when the candidate completes fewer
nets, fails where the baseline worked (timeout / worker error), or needs more than
25 % extra vias for the same completion. Wall-time changes are listed but never
counted: they depend on the machine and on what else was running. Runs are keyed
by (board id, mode), so a Speed file and an Accuracy file can be passed together.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

Key = tuple[str, str]


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("baseline", nargs="+", type=Path)
    ap.add_argument("--candidate", nargs="+", type=Path)
    ap.add_argument("--fail-on-regression", action="store_true")
    a = ap.parse_args(argv)
    base = load(a.baseline)
    summary(base, "baseline" if a.candidate else "run")
    if not a.candidate:
        return 0
    cand = load(a.candidate)
    summary(cand, "candidate")
    n = compare(base, cand)
    return 1 if (n and a.fail_on_regression) else 0


if __name__ == "__main__":
    sys.exit(main())
