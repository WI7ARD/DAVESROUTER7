"""Learning level 2 trainer: experience log -> frozen routing policy.

Pipeline::

    load records -> keep exploration data -> split by BOARD -> learn on train
    -> replay-evaluate on held-out boards -> save policy.json (+ report)

* **Exploration data only (default).** Records whose arms were chosen uniformly at
  random (``RandomPolicy``) are an unbiased sample of every arm in every context.
  Data logged by a learned policy is biased towards the arms it already liked.
* **Split by board, not by net.** Nets of one board share its layout, rules and
  congestion; splitting nets would leak the board into both halves and
  flatter the result.
* **Replay evaluation** (Li et al., 2011): with uniformly random logged arms,
  the attempts whose logged arm equals the policy's choice are an unbiased
  sample of what the policy would have got. The preset is scored the same way,
  so the two numbers are directly comparable. It is an estimate of per-attempt
  success, not of board completion: the Real100 A/B stays the acceptance test.

Run it with ``python -m pcbrouter.learning.trainer`` or ``tools/train_policy.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pcbrouter.learning.experience import ExperienceLog, default_dir
from pcbrouter.learning.policy import (
    PRESET,
    SCHEMA,
    FixedPolicy,
    Policy,
    ThompsonPolicy,
    attempts,
    learn,
)

REPORT_SCHEMA = "pcbrouter-policy-report/1"


@dataclass
class TrainConfig:
    #: which logging policies' records to learn from ("random" = exploration)
    policies: tuple[str, ...] = ("random",)
    #: fraction of boards held out for evaluation (0 = train on everything)
    holdout: float = 0.3
    seed: int = 0
    min_trials: int = 8
    margin: float = 0.05
    #: only records routed in these modes (None = all)
    modes: tuple[str, ...] | None = None


# ------------------------------------------------------------------- data
def load_records(folders: Iterable[Path]) -> list[dict[str, Any]]:
    """All records of the logs in *folders* (rotated file first, torn lines skipped)."""
    out: list[dict[str, Any]] = []
    for folder in folders:
        out.extend(ExperienceLog(folder).read())
    return out


def select(records: Iterable[dict[str, Any]], cfg: TrainConfig) -> list[dict[str, Any]]:
    """Records that can teach arms: logged by an allowed policy, with arms recorded."""
    keep = []
    for r in records:
        out = r.get("outcome") or {}
        if not out.get("arms") or out.get("policy") not in cfg.policies:
            continue
        if cfg.modes is not None and (r.get("settings") or {}).get("mode") not in cfg.modes:
            continue
        keep.append(r)
    return keep


def is_held_out(board: str, holdout: float, seed: int) -> bool:
    """Deterministic per board: the same board is always on the same side."""
    h = hashlib.sha256(f"{seed}:{board}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2.0**64 < holdout


def split_by_board(
    records: Sequence[dict[str, Any]], holdout: float, seed: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    train: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for r in records:
        (held if is_held_out(str(r.get("board")), holdout, seed) else train).append(r)
    return train, held


# ------------------------------------------------------------- evaluation
@dataclass
class Estimate:
    wins: float = 0.0
    n: int = 0

    @property
    def rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    def wilson95(self) -> tuple[float, float]:
        """95 % interval that stays sensible for small n and rates near 0 or 1."""
        if not self.n:
            return (0.0, 1.0)
        z, p, n = 1.96, self.rate, self.n
        mid = (p + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        return (max(0.0, mid - half), min(1.0, mid + half))


def replay(policy: Policy, records: Iterable[dict[str, Any]]) -> Estimate:
    """Per-attempt success of *policy* on uniformly-random logged data."""
    est = Estimate()
    for ctx, arm, won in attempts(records):
        if policy.choose(ctx, "", 0) == arm:
            est.n += 1
            est.wins += won
    return est


def compare(a: Estimate, b: Estimate) -> dict[str, Any]:
    """a - b with a normal-approximation 95 % interval and a plain verdict."""
    d = a.rate - b.rate
    if not a.n or not b.n:
        return {"delta": d, "ci95": None, "verdict": "no data"}
    se = math.sqrt(a.rate * (1 - a.rate) / a.n + b.rate * (1 - b.rate) / b.n)
    lo, hi = d - 1.96 * se, d + 1.96 * se
    verdict = "better" if lo > 0 else "worse" if hi < 0 else "no clear difference"
    return {"delta": d, "ci95": [lo, hi], "verdict": verdict}


def arm_costs(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Median seconds and expanded nodes per arm, from single-attempt nets (where
    the whole cost belongs to that one arm)."""
    secs: dict[str, list[float]] = {}
    nodes: dict[str, list[float]] = {}
    for r in records:
        out = r.get("outcome") or {}
        arms = out.get("arms") or []
        if len(arms) != 1:
            continue
        secs.setdefault(arms[0], []).append(float(out.get("route_s") or 0.0))
        nodes.setdefault(arms[0], []).append(float(out.get("expanded_nodes") or 0))
    return {
        a: {
            "nets": len(secs[a]),
            "median_s": statistics.median(secs[a]),
            "median_nodes": statistics.median(nodes[a]),
        }
        for a in sorted(secs)
    }


# ------------------------------------------------------------------ train
@dataclass
class TrainResult:
    policy: ThompsonPolicy
    report: dict[str, Any] = field(default_factory=dict)


def train(records: Sequence[dict[str, Any]], cfg: TrainConfig) -> TrainResult:
    usable = select(records, cfg)
    train_recs, held = split_by_board(usable, cfg.holdout, cfg.seed)
    pol = learn(train_recs)
    pol.min_trials, pol.margin = cfg.min_trials, cfg.margin

    changed = {}
    for ctx in sorted(pol.stats):
        arm = pol.choose(ctx, "", 0)
        if arm != PRESET:
            changed[ctx] = arm

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": asdict(cfg),
        "records": {
            "total": len(records),
            "usable": len(usable),
            "train": len(train_recs),
            "held_out": len(held),
        },
        "boards": {
            "train": len({r.get("board") for r in train_recs}),
            "held_out": len({r.get("board") for r in held}),
        },
        "contexts": len(pol.stats),
        "changed_contexts": changed,
        "arm_costs": arm_costs(usable),
    }
    if held:
        p, b = replay(pol, held), replay(FixedPolicy(), held)
        report["held_out"] = {
            "policy": {"rate": p.rate, "n": p.n, "ci95": p.wilson95()},
            "preset": {"rate": b.rate, "n": b.n, "ci95": b.wilson95()},
            "policy_minus_preset": compare(p, b),
        }
    return TrainResult(pol, report)


def save_policy(result: TrainResult, path: Path) -> None:
    """The policy JSON plus a small ``trained`` summary (counts only, no board ids)."""
    data = result.policy.to_json()
    r = result.report
    data["trained"] = {
        "created": r["created"],
        "config": r["config"],
        "records": r["records"],
        "boards": r["boards"],
        "held_out": r.get("held_out"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


# -------------------------------------------------------------------- CLI
def format_report(result: TrainResult) -> str:
    r, pol = result.report, result.policy
    rec, boards = r["records"], r["boards"]
    lines = [
        f"records: {rec['total']} total, {rec['usable']} usable "
        f"(train {rec['train']} on {boards['train']} boards, "
        f"held out {rec['held_out']} on {boards['held_out']} boards)",
        f"contexts: {r['contexts']}, changed from preset: {len(r['changed_contexts'])}",
        "",
        "context                          arms (wins/trials)",
    ]
    for ctx, arms in sorted(pol.stats.items()):
        row = "  ".join(f"{a}:{s.wins:g}/{s.trials:g}" for a, s in sorted(arms.items()))
        pick = r["changed_contexts"].get(ctx)
        lines.append(f"{ctx:32s} {row}" + (f"  -> {pick}" if pick else ""))
    lines += ["", "cost per arm (single-attempt nets): nets, median s, median nodes"]
    for a, c in r["arm_costs"].items():
        lines.append(f"  {a:12s} {c['nets']:6d} {c['median_s']:8.3f} {c['median_nodes']:10.0f}")
    h = r.get("held_out")
    if h:
        c = h["policy_minus_preset"]
        lines += [
            "",
            "held-out replay (per-attempt success):",
            f"  policy {h['policy']['rate']:.3f} (n={h['policy']['n']})"
            f"  preset {h['preset']['rate']:.3f} (n={h['preset']['n']})",
            f"  difference {c['delta']:+.3f}"
            + (f"  95% CI [{c['ci95'][0]:+.3f}, {c['ci95'][1]:+.3f}]" if c["ci95"] else "")
            + f"  -> {c['verdict']}",
        ]
    else:
        lines += ["", "no held-out boards: nothing to evaluate on (in-sample only)"]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="train_policy", description="Train a routing policy from experience logs."
    )
    p.add_argument(
        "--log",
        type=Path,
        action="append",
        default=None,
        help="experience folder (repeatable; default: the app's data folder)",
    )
    p.add_argument("--out", type=Path, default=Path("policy.json"))
    p.add_argument("--report", type=Path, default=None, help="also write a JSON report")
    p.add_argument(
        "--policies",
        default="random",
        help="comma-separated logging policies to learn from (default: random)",
    )
    p.add_argument("--mode", choices=("speed", "accuracy"), action="append", default=None)
    p.add_argument("--holdout", type=float, default=0.3, help="fraction of boards held out")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--min-trials", type=int, default=8)
    p.add_argument("--margin", type=float, default=0.05)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0.0 <= args.holdout < 1.0:
        print("--holdout must be in [0, 1)", file=sys.stderr)
        return 2
    cfg = TrainConfig(
        policies=tuple(x.strip() for x in args.policies.split(",") if x.strip()),
        holdout=args.holdout,
        seed=args.seed,
        min_trials=args.min_trials,
        margin=args.margin,
        modes=tuple(args.mode) if args.mode else None,
    )
    records = load_records(args.log or [default_dir()])
    result = train(records, cfg)
    if not result.report["records"]["train"]:
        print(
            f"no usable training records ({len(records)} read; need arms logged by "
            f"{', '.join(cfg.policies)})",
            file=sys.stderr,
        )
        return 2
    save_policy(result, args.out)
    if args.report is not None:
        args.report.write_text(json.dumps(result.report, indent=1), encoding="utf-8")
    print(format_report(result))
    print(f"\npolicy written to {args.out}")
    return 0


__all__ = [
    "SCHEMA",
    "Estimate",
    "TrainConfig",
    "TrainResult",
    "compare",
    "is_held_out",
    "load_records",
    "replay",
    "save_policy",
    "select",
    "split_by_board",
    "train",
]

if __name__ == "__main__":
    raise SystemExit(main())
