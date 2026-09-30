"""Paired A/B evaluation of routing strategies (fixed router vs a learned policy).

Both strategies route the *same* boards under the *same* load (they run side by
side), ``repeat`` times. Each (board, mode) then has one paired difference per
repeat, which cancels most of the board-to-board and machine-load variation
that makes single runs misleading on time-budgeted boards.

Classification of one (board, mode):

* ``unchanged`` — every paired delta is 0;
* ``improved`` / ``regressed`` — the mean delta is positive / negative, and
  either every repeat agrees in sign or the mean is more than two standard
  errors from 0;
* ``noisy`` — mixed signs within two standard errors.

Acceptance gate (all must hold for ``ACCEPTED``):

1. aggregate improvement — more nets in total, by at least 1 % of the
   baseline, and more boards improved than regressed;
2. no regressed board or guard board (reported separately);
3. no loss of validity — no more DRC errors on new copper than the baseline;
4. no more crashes / worker timeouts than the baseline;
5. reproducible — at least two repeats, and no improved board ever went the
   other way.

``REJECTED`` when 2, 3 or 4 fails; ``EXPERIMENTAL`` otherwise (no harm found,
but the benefit is not established).
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

Key = tuple[str, str]


def nets_done(r: dict[str, Any]) -> int:
    if r.get("status") != "ok":
        return 0
    return int((r.get("metrics") or {}).get("nets_completed", 0))


def nets_tried(r: dict[str, Any]) -> int:
    return int((r.get("metrics") or {}).get("nets_attempted", 0))


@dataclass
class Pair:
    """One (board, mode) across repeats."""

    key: Key
    base: list[int] = field(default_factory=list)
    cand: list[int] = field(default_factory=list)
    attempted: int = 0
    base_vias: list[int] = field(default_factory=list)
    cand_vias: list[int] = field(default_factory=list)
    base_wall: list[float] = field(default_factory=list)
    cand_wall: list[float] = field(default_factory=list)
    base_failures: int = 0  # worker crashes / hard timeouts
    cand_failures: int = 0
    base_drc: int = 0  # DRC errors on router-added copper
    cand_drc: int = 0
    decisions: list[str] = field(default_factory=list)

    @property
    def deltas(self) -> list[int]:
        return [c - b for b, c in zip(self.base, self.cand, strict=False)]

    @property
    def delta_mean(self) -> float:
        d = self.deltas
        return statistics.mean(d) if d else 0.0

    @property
    def delta_se(self) -> float:
        d = self.deltas
        return statistics.stdev(d) / math.sqrt(len(d)) if len(d) >= 2 else 0.0

    @property
    def verdict(self) -> str:
        d = self.deltas
        if not d or all(x == 0 for x in d):
            return "unchanged"
        m, se = self.delta_mean, self.delta_se
        if m > 0 and (all(x >= 0 for x in d) or m > 2 * se):
            return "improved"
        if m < 0 and (all(x <= 0 for x in d) or -m > 2 * se):
            return "regressed"
        return "noisy"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.key[0],
            "mode": self.key[1],
            "attempted": self.attempted,
            "base": self.base,
            "cand": self.cand,
            "deltas": self.deltas,
            "delta_mean": round(self.delta_mean, 3),
            "delta_se": round(self.delta_se, 3),
            "verdict": self.verdict,
            "runs": len(self.deltas),
            "vias_delta_mean": _mean_delta(self.base_vias, self.cand_vias),
            "wall_delta_mean_s": _mean_delta(self.base_wall, self.cand_wall),
            "base_failures": self.base_failures,
            "cand_failures": self.cand_failures,
            "base_drc_errors": self.base_drc,
            "cand_drc_errors": self.cand_drc,
            "decisions": sorted(set(self.decisions)),
        }


def _mean_delta(a: Sequence[float], b: Sequence[float]) -> float | None:
    d = [y - x for x, y in zip(a, b, strict=False)]
    return round(statistics.mean(d), 3) if d else None


def pair_runs(
    base_runs: Sequence[dict[Key, dict[str, Any]]], cand_runs: Sequence[dict[Key, dict[str, Any]]]
) -> list[Pair]:
    """Pair repeat *i* of the baseline with repeat *i* of the candidate (they ran
    side by side), per (board, mode) present in both."""
    pairs: dict[Key, Pair] = {}
    for base, cand in zip(base_runs, cand_runs, strict=True):
        for key in sorted(set(base) & set(cand)):
            b, c = base[key], cand[key]
            p = pairs.setdefault(key, Pair(key))
            p.attempted = max(p.attempted, nets_tried(b), nets_tried(c))
            p.base.append(nets_done(b))
            p.cand.append(nets_done(c))
            p.base_vias.append(int((b.get("metrics") or {}).get("new_vias") or 0))
            p.cand_vias.append(int((c.get("metrics") or {}).get("new_vias") or 0))
            p.base_wall.append(float(b.get("wall_s") or 0.0))
            p.cand_wall.append(float(c.get("wall_s") or 0.0))
            p.base_failures += b.get("status") != "ok"
            p.cand_failures += c.get("status") != "ok"
            p.base_drc += int(b.get("new_copper_drc_errors") or 0)
            p.cand_drc += int(c.get("new_copper_drc_errors") or 0)
            dec = c.get("policy_decision") or {}
            p.decisions.append(str(dec.get("decision", "NONE")))
    return [pairs[k] for k in sorted(pairs)]


def sign_test(pairs: Sequence[Pair]) -> float:
    """Two-sided exact sign test over boards with a non-zero mean delta."""
    pos = sum(1 for p in pairs if p.delta_mean > 0)
    neg = sum(1 for p in pairs if p.delta_mean < 0)
    n = pos + neg
    if n == 0:
        return 1.0
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def aggregate(pairs: Sequence[Pair]) -> dict[str, Any]:
    by = Counter(p.verdict for p in pairs)
    base_total = sum(statistics.mean(p.base) for p in pairs if p.base)
    cand_total = sum(statistics.mean(p.cand) for p in pairs if p.cand)
    return {
        "boards": len(pairs),
        "improved": by["improved"],
        "unchanged": by["unchanged"],
        "regressed": by["regressed"],
        "noisy": by["noisy"],
        "base_nets_mean": round(base_total, 2),
        "cand_nets_mean": round(cand_total, 2),
        "nets_gained": round(sum(max(0.0, p.delta_mean) for p in pairs), 2),
        "nets_lost": round(sum(min(0.0, p.delta_mean) for p in pairs), 2),
        "net_delta": round(cand_total - base_total, 2),
        "sign_test_p": round(sign_test(pairs), 4),
        "vias_delta": round(sum(v for p in pairs if (v := _vd(p)) is not None), 2),
        "wall_delta_s": round(
            sum(w for p in pairs if (w := _mean_delta(p.base_wall, p.cand_wall)) is not None), 1
        ),
        "base_failures": sum(p.base_failures for p in pairs),
        "cand_failures": sum(p.cand_failures for p in pairs),
        "base_drc_errors": sum(p.base_drc for p in pairs),
        "cand_drc_errors": sum(p.cand_drc for p in pairs),
        "decisions": dict(Counter(d for p in pairs for d in p.decisions)),
    }


def _vd(p: Pair) -> float | None:
    return _mean_delta(p.base_vias, p.cand_vias)


def gate(pairs: Sequence[Pair], guard: Sequence[Pair] = (), repeats: int = 1) -> dict[str, Any]:
    """The acceptance gate (see module docstring). Guard boards are reported
    separately and count for rule 2 on their own."""
    agg = aggregate(pairs)
    gagg = aggregate(guard) if guard else None
    base = agg["base_nets_mean"] or 1.0
    checks = {
        "1_aggregate_improves": agg["net_delta"] >= 0.01 * base
        and agg["improved"] > agg["regressed"],
        "2_no_regressed_board": agg["regressed"] == 0,
        "2_no_regressed_guard_board": gagg is None or gagg["regressed"] == 0,
        "3_validity_not_reduced": agg["cand_drc_errors"] <= agg["base_drc_errors"]
        and (gagg is None or gagg["cand_drc_errors"] <= gagg["base_drc_errors"]),
        "4_no_more_failures": agg["cand_failures"] <= agg["base_failures"]
        and (gagg is None or gagg["cand_failures"] <= gagg["base_failures"]),
        "5_reproducible": repeats >= 2
        and all(min(p.deltas) >= 0 for p in pairs if p.verdict == "improved"),
    }
    harm = not (
        checks["2_no_regressed_board"]
        and checks["2_no_regressed_guard_board"]
        and checks["3_validity_not_reduced"]
        and checks["4_no_more_failures"]
    )
    status = "REJECTED" if harm else "ACCEPTED" if all(checks.values()) else "EXPERIMENTAL"
    return {"status": status, "checks": checks, "aggregate": agg, "guard": gagg}


def guard_rows(results: Sequence[dict[str, Any]]) -> dict[Key, dict[str, Any]]:
    """``tools/run_benchmarks.py`` results.json rows -> Real100-like rows."""
    out: dict[Key, dict[str, Any]] = {}
    for r in results:
        done, _, tried = str(r.get("nets", "0/0")).partition("/")
        ok = r.get("exit_code") in (0, 3) and done.isdigit()
        out[(str(r["board"]), str(r["mode"]))] = {
            "status": "ok" if ok else f"exit {r.get('exit_code')}",
            "metrics": {
                "nets_completed": int(done) if done.isdigit() else 0,
                "nets_attempted": int(tried) if tried.isdigit() else 0,
                "new_vias": r.get("vias") or 0,
            },
            "wall_s": r.get("wall_s") or 0.0,
            "new_copper_drc_errors": 0 if r.get("verified") in (True, None) else 1,
        }
    return out


def format_pairs(pairs: Sequence[Pair], title: str) -> list[str]:
    lines = [f"{title}: {len(pairs)} board/mode pairs"]
    for p in pairs:
        if p.verdict == "unchanged":
            continue
        lines.append(
            f"  {p.verdict:<9} {p.key[0]:<14} {p.key[1]:<8} base {p.base} cand {p.cand} "
            f"of {p.attempted}  mean {p.delta_mean:+.2f}"
            + (f" ±{p.delta_se:.2f}" if len(p.deltas) >= 2 else "")
            + (f"  [{', '.join(sorted(set(p.decisions)))}]" if p.decisions else "")
        )
    return lines
