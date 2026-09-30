"""Learning level 2: choose a per-net search variant ("arm") by kind of net.

Every arm is a small, individually safe change to the net's search request. The
exact validator still decides every route, so an arm can only change how fast
and how often a net routes, never whether the copper is legal.

* :class:`FixedPolicy` — always the preset (the router's default behaviour).
* :class:`RandomPolicy` — seeded uniform choice: exploration runs that give
  every arm outcomes on every kind of net (the data a learner needs).
* :class:`ThompsonPolicy` — per context bucket, a Beta posterior of "routed in
  its slice" per arm; ``frozen=True`` picks the posterior mean
  (deterministic, for benchmarks), otherwise it samples (keeps exploring).

A policy is learned from the experience log with :func:`learn` and saved as JSON.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pcbrouter.routing.request import RouteRequest

SCHEMA = "pcbrouter-policy/1"
PRESET = "preset"


def _no_coarse(r: RouteRequest) -> RouteRequest:
    return replace(r, coarse_factor=0)


def _finer_grid(r: RouteRequest) -> RouteRequest:
    return replace(r, grid_resolution=max(50_000, r.grid_resolution // 2))


def _greedier(r: RouteRequest) -> RouteRequest:
    return replace(r, heuristic_weight=max(1.25, r.heuristic_weight * 1.25))


def _fewer_vias(r: RouteRequest) -> RouteRequest:
    return replace(r, minimize_vias=True)


#: arm name -> request transform (all safe on their own; the validator gates)
ARMS: dict[str, Callable[[RouteRequest], RouteRequest]] = {
    PRESET: lambda r: r,
    "no_coarse": _no_coarse,
    "finer_grid": _finer_grid,
    "greedier": _greedier,
    "fewer_vias": _fewer_vias,
}


def _band(x: float, edges: tuple[float, ...]) -> int:
    return sum(x > e for e in edges)


def bucket(kind: str, pads: int, escape_options: int, layers: int) -> str:
    """A coarse context: enough to separate e.g. fine-pitch escapes on 4 layers
    from long 2-pad signals on 2 layers, few enough buckets to learn from little data."""
    return (
        f"{kind}|p{_band(pads, (2, 4, 12))}|e{_band(escape_options, (2, 6, 20))}"
        f"|l{_band(layers, (2, 4))}"
    )


def task_bucket(task: Any, layers: int) -> str:
    kind = getattr(task.kind, "value", str(task.kind))
    return bucket(kind, task.pads, task.escape_options, layers)


class Policy:
    name = "fixed"

    def choose(self, ctx: str, net: str, attempt: int) -> str:
        return PRESET

    def apply(self, arm: str, request: RouteRequest) -> RouteRequest:
        return ARMS.get(arm, ARMS[PRESET])(request)


class FixedPolicy(Policy):
    pass


class RandomPolicy(Policy):
    """Deterministic per (seed, net, attempt): reproducible exploration."""

    name = "random"

    def __init__(self, seed: int = 0, arms: tuple[str, ...] = tuple(ARMS)) -> None:
        self.seed, self.arms = seed, arms

    def choose(self, ctx: str, net: str, attempt: int) -> str:
        h = hashlib.sha256(f"{self.seed}:{net}:{attempt}".encode()).digest()
        return self.arms[int.from_bytes(h[:4], "big") % len(self.arms)]


@dataclass
class ArmStats:
    wins: float = 0.0
    trials: float = 0.0

    def mean(self, prior: tuple[float, float] = (1.0, 1.0)) -> float:
        a, b = prior
        return (self.wins + a) / (self.trials + a + b)


@dataclass
class ThompsonPolicy(Policy):
    stats: dict[str, dict[str, ArmStats]] = field(default_factory=dict)
    frozen: bool = True
    seed: int = 0
    #: an arm must beat the preset's mean by this much to replace it (frozen)
    margin: float = 0.05
    min_trials: int = 8
    name = "thompson"

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def choose(self, ctx: str, net: str, attempt: int) -> str:
        arms = self.stats.get(ctx)
        if not arms:
            return PRESET
        if self.frozen:
            base = arms.get(PRESET, ArmStats()).mean()
            best, best_mean = PRESET, base
            for arm, st in arms.items():
                if arm in ARMS and st.trials >= self.min_trials and st.mean() > best_mean:
                    best, best_mean = arm, st.mean()
            return best if best == PRESET or best_mean >= base + self.margin else PRESET
        draws = {
            arm: self._rng.betavariate(st.wins + 1.0, st.trials - st.wins + 1.0)
            for arm, st in arms.items()
            if arm in ARMS
        }
        return max(draws, key=draws.__getitem__) if draws else PRESET

    # ------------------------------------------------------------ persistence
    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "margin": self.margin,
            "min_trials": self.min_trials,
            "stats": {
                c: {a: [s.wins, s.trials] for a, s in arms.items()}
                for c, arms in sorted(self.stats.items())
            },
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], *, frozen: bool = True) -> ThompsonPolicy:
        if data.get("schema") != SCHEMA:
            raise ValueError(f"not a routing policy: {data.get('schema')!r}")
        stats = {
            c: {a: ArmStats(float(w), float(t)) for a, (w, t) in arms.items()}
            for c, arms in data["stats"].items()
        }
        return cls(
            stats,
            frozen=frozen,
            margin=float(data.get("margin", 0.05)),
            min_trials=int(data.get("min_trials", 8)),
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, *, frozen: bool = True) -> ThompsonPolicy:
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")), frozen=frozen)


def attempt_context(base: str, attempt: int) -> str:
    """First attempts (pass 1, small slice) and retries (bigger slice) are learned
    separately, so an arm's effect is not confused with the pass's budget."""
    return f"{base}|{'first' if attempt == 0 else 'retry'}"


def learn(records: Iterable[dict[str, Any]]) -> ThompsonPolicy:
    """Per (context, arm) over *attempts*: every attempt is a trial for the arm it
    used; only the last attempt of a routed net is a win. Records without arms
    (fixed-policy runs) teach nothing about arms and are skipped."""
    stats: dict[str, dict[str, ArmStats]] = {}
    for r in records:
        out, nf = r.get("outcome") or {}, r.get("net_f") or {}
        arms = out.get("arms") or []
        if not arms:
            continue
        base = nf.get("bucket") or bucket(
            nf.get("kind", "signal"),
            int(nf.get("pads", 0)),
            int(nf.get("escape_options", 99)),
            int((r.get("board_f") or {}).get("copper_layers", 2)),
        )
        routed = out.get("status") == "SUCCESS"
        for i, arm in enumerate(arms):
            st = stats.setdefault(attempt_context(base, i), {}).setdefault(arm, ArmStats())
            st.trials += 1
            st.wins += 1.0 if routed and i == len(arms) - 1 else 0.0
    return ThompsonPolicy(stats)


def policy_from_spec(spec: str | None) -> Policy | None:
    """``None``/"fixed" -> None (preset), "random:SEED", or a policy JSON path."""
    if not spec or spec == "fixed":
        return None
    if spec.startswith("random"):
        _, _, seed = spec.partition(":")
        return RandomPolicy(int(seed or 0))
    return ThompsonPolicy.load(Path(spec))
