"""Learning level 2 trainer: experience log -> frozen routing policy.

Pipeline::

    load records -> keep exploration data -> split by BOARD -> learn on train
    -> replay-evaluate on held-out boards -> save policy.json (+ report)

* **Exploration data only (default).** Records whose arms were chosen uniformly at
  random (``RandomPolicy``) are an unbiased sample of every arm in every context.
  Data logged by a learned policy is biased towards the arms it already liked.
* **Split by board group, not by net.** Nets of one board share its layout, rules
  and congestion; splitting nets would leak the board into both halves. Boards
  whose profiles are near-identical (variants of one design, e.g. Real100 K067
  and K088) are grouped and kept on the same side for the same reason.
* **Support and evidence.** The policy file also stores where it was trained
  (the training boards' profiles) and, with ``--evidence``, board-level A/B
  results on those boards. ``learning.selector`` uses both to decide per board
  whether to use the policy or fall back to the fixed router.
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

import numpy as np

from pcbrouter.learning.experience import ExperienceLog, default_dir
from pcbrouter.learning.features import transform
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
    #: boards closer than this (standardized profile distance) are one group
    dup_radius: float = 1.0
    #: selector: out-of-distribution radius quantile, and whether board-level
    #: A/B evidence on similar boards is required before the policy is used
    ood_quantile: float = 0.75
    require_evidence: bool = True


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


def board_profiles(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """board id -> recorded profile (schema-2 records; the first one seen)."""
    out: dict[str, dict[str, float]] = {}
    for r in records:
        b, prof = str(r.get("board")), r.get("board_p")
        if prof and b not in out:
            out[b] = prof
    return out


def board_groups(records: Sequence[dict[str, Any]], dup_radius: float = 1.0) -> dict[str, str]:
    """board id -> group id. Boards with near-identical profiles (standardized
    distance < *dup_radius*) share a group; boards without a profile are alone."""
    boards = sorted({str(r.get("board")) for r in records})
    profiles = board_profiles(records)
    parent = {b: b for b in boards}

    def find(b: str) -> str:
        while parent[b] != b:
            parent[b] = parent[parent[b]]
            b = parent[b]
        return b

    known = [b for b in boards if b in profiles]
    if len(known) >= 2:
        x = np.array([transform(profiles[b]) for b in known])
        z = (x - x.mean(axis=0)) / np.maximum(x.std(axis=0), 0.1)
        d = np.sqrt(((z[:, None, :] - z[None, :, :]) ** 2).sum(-1))
        for i in range(len(known)):
            for j in range(i + 1, len(known)):
                if d[i, j] < dup_radius:
                    a, b = find(known[i]), find(known[j])
                    parent[max(a, b)] = min(a, b)
    return {b: find(b) for b in boards}


def split_by_board(
    records: Sequence[dict[str, Any]], holdout: float, seed: int, dup_radius: float = 1.0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Train / held-out records, split by board group (see :func:`board_groups`)."""
    groups = board_groups(records, dup_radius)
    train: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for r in records:
        g = groups[str(r.get("board"))]
        (held if is_held_out(g, holdout, seed) else train).append(r)
    return train, held


def dataset_id(records: Iterable[dict[str, Any]]) -> str:
    """Content hash of the training records (order-independent)."""
    lines = sorted(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records)
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode())
        h.update(b"\n")
    return "data-" + h.hexdigest()[:12]


def load_evidence(path: Path) -> list[dict[str, Any]]:
    """Board-level A/B results (``tools/real100_compare.py --json``): entries with
    ``profile``, ``mode`` and ``delta_mean`` (candidate minus fixed nets)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [b for b in data.get("boards", []) if b.get("profile") and "delta_mean" in b]


def attach_evidence(support: Any, evidence: Sequence[dict[str, Any]]) -> int:
    """Put each A/B result on the training board it was measured on (same
    profile). Results for boards the policy was not trained on are ignored:
    evidence must never come from evaluation boards. Returns how many attached."""
    attached = 0
    zs = np.array([b.z for b in support.boards])
    for e in evidence:
        z = support.standardize(e["profile"])
        d = np.sqrt(((zs - z) ** 2).sum(axis=1))
        i = int(np.argmin(d))
        if d[i] < 0.05:
            support.boards[i].evidence[str(e["mode"])] = {
                "delta": round(float(e["delta_mean"]), 3),
                "runs": int(e.get("runs", 1)),
            }
            attached += 1
    return attached


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
    support: Any = None


def train(
    records: Sequence[dict[str, Any]],
    cfg: TrainConfig,
    evidence: Sequence[dict[str, Any]] = (),
) -> TrainResult:
    from pcbrouter.learning.selector import fit_support

    usable = select(records, cfg)
    groups = board_groups(usable, cfg.dup_radius)
    train_recs, held = split_by_board(usable, cfg.holdout, cfg.seed, cfg.dup_radius)
    pol = learn(train_recs)
    pol.min_trials, pol.margin = cfg.min_trials, cfg.margin
    pol.trained_modes = cfg.modes
    train_profiles = board_profiles(train_recs)
    support = None
    attached = 0
    if train_profiles:
        support = fit_support(
            [train_profiles[b] for b in sorted(train_profiles)],
            quantile=cfg.ood_quantile,
            require_evidence=cfg.require_evidence,
        )
        attached = attach_evidence(support, evidence)
    merged = [
        sorted(b for b in groups if groups[b] == g)
        for g in sorted(set(groups.values()))
        if sum(1 for v in groups.values() if v == g) > 1
    ]

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
        "groups": {"boards": len(groups), "groups": len(set(groups.values())), "merged": merged},
        "held_out_boards": sorted({str(r.get("board")) for r in held}),
        "train_boards": sorted({str(r.get("board")) for r in train_recs}),
        "dataset_id": dataset_id(train_recs),
        "policy_id": pol.policy_id(),
        "support": (
            {
                "boards": len(support.boards),
                "radius": round(support.radius, 3),
                "evidence_attached": attached,
            }
            if support is not None
            else None
        ),
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
    return TrainResult(pol, report, support)


def save_policy(result: TrainResult, path: Path) -> None:
    """The policy JSON plus a small ``trained`` summary (counts only, no board ids)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy_json(result), indent=1), encoding="utf-8")


def save_policy_set(results: dict[str, TrainResult], path: Path) -> None:
    """One policy per mode in a single file (``pcbrouter-policy-set/1``)."""
    from pcbrouter.learning.policy import SET_SCHEMA

    data = {"schema": SET_SCHEMA, "modes": {m: policy_json(r) for m, r in results.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def policy_json(result: TrainResult) -> dict[str, Any]:
    data = result.policy.to_json()
    r = result.report
    data["trained"] = {
        "created": r["created"],
        "policy_id": r["policy_id"],
        "dataset_id": r["dataset_id"],
        "config": r["config"],
        "records": r["records"],
        "boards": r["boards"],
        "held_out": r.get("held_out"),
    }
    if result.support is not None:
        data["support"] = result.support.to_json()
    return data


# -------------------------------------------------------------------- CLI
def format_report(result: TrainResult) -> str:
    r, pol = result.report, result.policy
    rec, boards = r["records"], r["boards"]
    lines = [
        f"policy {r['policy_id']}  dataset {r['dataset_id']}",
        f"board groups: {r['groups']['groups']} from {r['groups']['boards']} boards "
        f"({len(r['groups']['merged'])} merged near-duplicate group(s))",
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
    p.add_argument(
        "--evidence",
        type=Path,
        action="append",
        default=None,
        help="board-level A/B results (real100_compare --json) on TRAINING boards",
    )
    p.add_argument(
        "--no-require-evidence",
        action="store_true",
        help="use the policy on in-distribution boards even without A/B evidence",
    )
    p.add_argument(
        "--per-mode",
        action="store_true",
        help="train Speed and Accuracy separately and write one policy-set file",
    )
    p.add_argument("--dup-radius", type=float, default=1.0)
    p.add_argument("--ood-quantile", type=float, default=0.75)
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
        dup_radius=args.dup_radius,
        ood_quantile=args.ood_quantile,
        require_evidence=not args.no_require_evidence,
    )
    records = load_records(args.log or [default_dir()])
    evidence = [e for path in args.evidence or [] for e in load_evidence(path)]
    if args.per_mode:
        from dataclasses import replace

        results = {}
        for m in ("speed", "accuracy"):
            res = train(
                records, replace(cfg, modes=(m,)), [e for e in evidence if e.get("mode") == m]
            )
            if not res.report["records"]["train"]:
                print(f"no usable training records for {m} mode", file=sys.stderr)
                return 2
            results[m] = res
            print(f"== {m}")
            print(format_report(res))
        save_policy_set(results, args.out)
        if args.report is not None:
            args.report.write_text(
                json.dumps({m: r.report for m, r in results.items()}, indent=1), encoding="utf-8"
            )
        print(f"\npolicy set written to {args.out}")
        return 0
    if cfg.modes:
        evidence = [e for e in evidence if e.get("mode") in cfg.modes]
    result = train(records, cfg, evidence)
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
