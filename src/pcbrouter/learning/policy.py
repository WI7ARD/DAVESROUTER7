"""Learning level 2: choose a per-net search variant ("arm") by kind of net.

Every arm is a small, individually safe change to the net's search request. The
exact validator still decides every route, so an arm can only change how fast
and how often a net routes, never whether the copper is legal.

* :class:`FixedPolicy` — always the preset (the router's default behaviour).
* :class:`RandomPolicy` — seeded uniform choice: exploration runs that give
  every arm outcomes on every kind of net (the data a learner needs).
* :class:`ThompsonPolicy` — per context, success counts and attempt costs per
  arm. ``frozen=True`` (benchmarks, the app) replaces the preset only on
  evidence that survives the sample size (see :meth:`ThompsonPolicy.choose`);
  otherwise it samples the posterior (keeps exploring).

A policy is learned from the experience log with :func:`learn` and saved as JSON,
stamped with the router it was learned on (:func:`router_signature`).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pcbrouter.routing.request import RouteRequest

SCHEMA = "pcbrouter-policy/1"
PRESET = "preset"

#: the Speed preset promises a path cost at most 1.5x optimal; no arm may break it
MAX_HEURISTIC_WEIGHT = 1.5
#: routing-demand bands (airwire length per routable area and layer, see
#: learning.features): the tertiles of the 41 routable Real100 boards
#: (0.028 / 0.132, rounded), so each band holds about a third of the corpus
DEMAND_EDGES = (0.03, 0.13)
#: 95 % two-sided normal quantile for the Wilson intervals
Z95 = 1.96


def _no_coarse(r: RouteRequest) -> RouteRequest:
    return replace(r, coarse_factor=0)


def _finer_grid(r: RouteRequest) -> RouteRequest:
    return replace(r, grid_resolution=max(50_000, r.grid_resolution // 2))


def _greedier(r: RouteRequest) -> RouteRequest:
    # capped at the proven bound: on Speed (already 1.5) this is a no-op, and the
    # router then records the attempt as the preset (see BoardRouter)
    w = min(MAX_HEURISTIC_WEIGHT, max(1.25, r.heuristic_weight * 1.25))
    return replace(r, heuristic_weight=max(r.heuristic_weight, w))


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


def bucket(
    kind: str, pads: int, escape_options: int, layers: int, demand: float | None = None
) -> str:
    """A coarse context: enough to separate e.g. fine-pitch escapes on 4 layers
    from long 2-pad signals on 2 layers, few enough buckets to learn from little data.

    *layers* is the number of **routable** layers (copper layers minus the ones a
    zone mostly covers, see ``learning.features``): a 4-layer board with two
    planes routes like a 2-layer board. *demand* (the board's routing demand)
    separates a 2-pad signal on a sparse board from one on a crowded board;
    unknown demand (old records) gives ``d?``, which no router context matches."""
    d = "?" if demand is None else str(_band(demand, DEMAND_EDGES))
    return (
        f"{kind}|p{_band(pads, (2, 4, 12))}|e{_band(escape_options, (2, 6, 20))}"
        f"|l{_band(layers, (2, 4))}|d{d}"
    )


def task_bucket(task: Any, layers: int, demand: float | None = None) -> str:
    kind = getattr(task.kind, "value", str(task.kind))
    return bucket(kind, task.pads, task.escape_options, layers, demand)


#: failure reasons -> retry context: a retry after running out of search budget
#: wants different help (more budget, cheaper search) than one after NO_PATH
#: (finer grid, other layers)
_LIMIT_REASONS = {"TIMEOUT", "VIA_LIMIT", "LAYER_RESTRICTION", "NODE_LIMIT", "CANCELLED"}


def retry_kind(reason: str | None) -> str:
    if reason is None:
        return "unknown"
    reason = str(getattr(reason, "value", reason)).upper()
    if reason in _LIMIT_REASONS:
        return "limit"
    if reason == "NO_PATH":
        return "no_path"
    return "other"


def attempt_context(base: str, attempt: int, prev_reason: str | None = None) -> str:
    """First attempts (pass 1, small slice) and retries are learned separately,
    and retries by why the previous attempt failed."""
    if attempt == 0:
        return f"{base}|first"
    return f"{base}|retry:{retry_kind(prev_reason)}"


def router_signature() -> dict[str, str]:
    """What a policy's statistics describe: the app and the engine versions whose
    grids, clearances and search produced them."""
    from pcbrouter import __version__
    from pcbrouter.geometry import GEOMETRY_ENGINE_VERSION
    from pcbrouter.rules.ruleset import RULE_ENGINE_VERSION

    return {"app": __version__, "geometry": GEOMETRY_ENGINE_VERSION, "rules": RULE_ENGINE_VERSION}


class Policy:
    name = "fixed"

    def choose(self, ctx: str, net: str, attempt: int) -> str:
        return PRESET

    def apply(self, arm: str, request: RouteRequest) -> RouteRequest:
        return ARMS.get(arm, ARMS[PRESET])(request)

    def compatibility(self) -> str | None:
        """None when the policy fits this router, else the reason it does not."""
        return None


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


def wilson95(wins: float, trials: float) -> tuple[float, float]:
    """95 % Wilson interval of a success rate (sensible for small n and rates near
    0 or 1). No trials: the whole range."""
    if trials <= 0:
        return (0.0, 1.0)
    z, n = Z95, trials
    p = wins / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, mid - half), min(1.0, mid + half))


@dataclass
class ArmStats:
    wins: float = 0.0
    trials: float = 0.0
    #: median seconds per attempt, and how many attempts it is based on
    cost_s: float | None = None
    cost_n: int = 0

    def mean(self, prior: tuple[float, float] = (1.0, 1.0)) -> float:
        a, b = prior
        return (self.wins + a) / (self.trials + a + b)

    def interval(self) -> tuple[float, float]:
        return wilson95(self.wins, self.trials)


@dataclass
class ThompsonPolicy(Policy):
    stats: dict[str, dict[str, ArmStats]] = field(default_factory=dict)
    frozen: bool = True
    seed: int = 0
    #: kept for old policy files; decisions use the Wilson rule below instead
    margin: float = 0.05
    min_trials: int = 8
    #: "much cheaper": median attempt cost at most this share of the preset's
    cheap_ratio: float = 0.5
    #: the modes the training data came from (None = any / unknown)
    trained_modes: tuple[str, ...] | None = None
    #: router_signature() of the router the statistics came from (None = unknown)
    router: dict[str, str] | None = None
    name = "thompson"

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # ---------------------------------------------------------------- choice
    def eligible(self, ctx: str) -> dict[str, str]:
        """Arms allowed to replace the preset in *ctx*, with the reason:

        * **routes significantly more** — the arm's 95 % Wilson lower bound is
          above the preset's upper bound (a lead that survives the sample size:
          on 8 trials the interval is about ±0.3, so small leads never count);
        * **much cheaper at no loss** — median attempt cost at most
          ``cheap_ratio`` of the preset's, and a success rate at least the
          preset's.

        Both need at least ``min_trials`` attempts of the arm."""
        arms = self.stats.get(ctx) or {}
        base = arms.get(PRESET, ArmStats())
        _, base_hi = base.interval()
        out: dict[str, str] = {}
        for arm, st in arms.items():
            if arm == PRESET or arm not in ARMS or st.trials < self.min_trials:
                continue
            lo, _ = st.interval()
            if lo > base_hi:
                out[arm] = "routes significantly more"
            elif (
                st.cost_s is not None
                and base.cost_s
                and st.cost_s <= self.cheap_ratio * base.cost_s
                and st.mean() >= base.mean()
            ):
                out[arm] = "much cheaper at no loss"
        return out

    def value(self, ctx: str, arm: str, p: float | None = None) -> float:
        """Cost-discounted success: ``p * tau / (tau + cost)``, tau = the preset's
        median cost here (so the preset at its own cost scores p/2). Board jobs
        run under time budgets: routing slightly more nets at ten times the time
        should not win."""
        arms = self.stats.get(ctx) or {}
        st = arms.get(arm, ArmStats())
        p = st.mean() if p is None else p
        tau = (arms.get(PRESET) or ArmStats()).cost_s
        if not tau or st.cost_s is None:
            return p
        return p * tau / (tau + st.cost_s)

    def choose(self, ctx: str, net: str, attempt: int) -> str:
        arms = self.stats.get(ctx)
        if not arms:
            return PRESET
        if self.frozen:
            ok = self.eligible(ctx)
            if not ok:
                return PRESET
            best = max(ok, key=lambda a: (self.value(ctx, a), a))
            return best if self.value(ctx, best) >= self.value(ctx, PRESET) else PRESET
        draws = {
            arm: self.value(
                ctx, arm, self._rng.betavariate(st.wins + 1.0, st.trials - st.wins + 1.0)
            )
            for arm, st in arms.items()
            if arm in ARMS
        }
        return max(draws, key=draws.__getitem__) if draws else PRESET

    def compatibility(self) -> str | None:
        if self.router is None:
            return None
        here = router_signature()
        diff = [
            f"{k} {self.router.get(k)} vs {v}" for k, v in here.items() if self.router.get(k) != v
        ]
        if diff:
            return "policy was trained on another router (" + ", ".join(diff) + "); retrain it"
        return None

    # ------------------------------------------------------------ persistence
    def to_json(self) -> dict[str, Any]:
        def enc(s: ArmStats) -> list[Any]:
            return (
                [s.wins, s.trials] if s.cost_s is None else [s.wins, s.trials, s.cost_s, s.cost_n]
            )

        return {
            "schema": SCHEMA,
            "margin": self.margin,
            "min_trials": self.min_trials,
            "cheap_ratio": self.cheap_ratio,
            "decision_rule": "wilson95+cost/1",
            "router": self.router,
            "stats": {
                c: {a: enc(s) for a, s in sorted(arms.items())}
                for c, arms in sorted(self.stats.items())
            },
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], *, frozen: bool = True) -> ThompsonPolicy:
        if data.get("schema") != SCHEMA:
            raise ValueError(f"not a routing policy: {data.get('schema')!r}")

        def dec(v: list[Any]) -> ArmStats:
            if len(v) >= 4:
                return ArmStats(float(v[0]), float(v[1]), float(v[2]), int(v[3]))
            return ArmStats(float(v[0]), float(v[1]))

        stats = {c: {a: dec(v) for a, v in arms.items()} for c, arms in data["stats"].items()}
        trained = data.get("trained") or {}
        modes = (trained.get("config") or {}).get("modes")
        return cls(
            stats,
            frozen=frozen,
            margin=float(data.get("margin", 0.05)),
            min_trials=int(data.get("min_trials", 8)),
            cheap_ratio=float(data.get("cheap_ratio", 0.5)),
            trained_modes=tuple(modes) if modes else None,
            router=data.get("router") or trained.get("router"),
        )

    def policy_id(self) -> str:
        """Content hash of what drives decisions (stats + thresholds + router):
        two files with the same id choose identically."""
        body = {k: v for k, v in self.to_json().items() if k != "trained"}
        blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return "pol-" + hashlib.sha256(blob).hexdigest()[:12]

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, *, frozen: bool = True) -> ThompsonPolicy:
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")), frozen=frozen)


def record_layers(r: dict[str, Any]) -> int:
    """Routable layers of a record's board: the profile's signal layers when
    recorded (schema 2), else the copper layer count (schema 1 records)."""
    prof = r.get("board_p") or {}
    if prof.get("signal_layers"):
        return int(prof["signal_layers"])
    return int((r.get("board_f") or {}).get("copper_layers", 2))


def record_demand(r: dict[str, Any]) -> float | None:
    prof = r.get("board_p") or {}
    d = prof.get("demand")
    return float(d) if d is not None else None


@dataclass(frozen=True)
class Attempt:
    ctx: str
    arm: str
    #: 1 when this attempt routed the net (only ever the last attempt)
    won: float
    #: seconds this attempt took; None when the record cannot attribute it
    cost_s: float | None


def attempts(records: Iterable[dict[str, Any]]) -> Iterator[Attempt]:
    """Every logged attempt with its context. Contexts are derived here from the
    raw recorded features (never from a stored context), so old logs stay usable
    when the context definition changes. Records without arms (fixed-policy
    runs) teach nothing about arms and are skipped."""
    for r in records:
        out, nf = r.get("outcome") or {}, r.get("net_f") or {}
        arms = out.get("arms") or []
        if not arms:
            continue
        base = bucket(
            nf.get("kind", "signal"),
            int(nf.get("pads", 0)),
            int(nf.get("escape_options", 99)),
            record_layers(r),
            record_demand(r),
        )
        trace = out.get("trace") or []
        routed = out.get("status") == "SUCCESS"
        for i, arm in enumerate(arms):
            prev = trace[i - 1].get("reason") if 0 < i <= len(trace) else None
            if i < len(trace) and trace[i].get("route_s") is not None:
                cost: float | None = float(trace[i]["route_s"])
            elif len(arms) == 1 and out.get("route_s") is not None:
                cost = float(out["route_s"])
            else:
                cost = None
            won = 1.0 if routed and i == len(arms) - 1 else 0.0
            yield Attempt(attempt_context(base, i, prev), arm, won, cost)


def learn(records: Iterable[dict[str, Any]]) -> ThompsonPolicy:
    """Per (context, arm) over attempts: every attempt is a trial for its arm and
    a win when it routed the net; attributable attempt times give the arm's
    median cost (see :func:`attempts`)."""
    stats: dict[str, dict[str, ArmStats]] = {}
    costs: dict[tuple[str, str], list[float]] = {}
    for a in attempts(records):
        st = stats.setdefault(a.ctx, {}).setdefault(a.arm, ArmStats())
        st.trials += 1
        st.wins += a.won
        if a.cost_s is not None:
            costs.setdefault((a.ctx, a.arm), []).append(a.cost_s)
    for (ctx, arm), cs in costs.items():
        stats[ctx][arm].cost_s = round(statistics.median(cs), 4)
        stats[ctx][arm].cost_n = len(cs)
    return ThompsonPolicy(stats, router=router_signature())


SET_SCHEMA = "pcbrouter-policy-set/1"


def builtin_policy_path() -> Path | None:
    """The policy set shipped with the app (trained on Real100), if any."""
    p = Path(__file__).parent / "data" / "policy_real100.json"
    return p if p.is_file() else None


class PolicySet(Policy):
    """One policy per routing mode (the same arm means different things on top of
    the Speed and Accuracy presets). The router asks :meth:`for_mode`."""

    name = "set"

    def __init__(self, policies: dict[str, Policy], status: str | None = None) -> None:
        self.policies = policies
        #: the acceptance-gate status recorded in the file, if any
        self.status = status

    def for_mode(self, mode: str | None) -> Policy | None:
        return self.policies.get(mode or "")

    def policy_id(self) -> str:
        parts = []
        for mode, pol in sorted(self.policies.items()):
            pid = getattr(pol, "policy_id", None)
            parts.append(f"{mode}={pid() if callable(pid) else pol.name}")
        return "set-" + hashlib.sha256(";".join(parts).encode()).hexdigest()[:12]


def _policy_from_json(data: dict[str, Any]) -> Policy:
    inner = ThompsonPolicy.from_json(data)
    if data.get("support"):
        from pcbrouter.learning.selector import SelectivePolicy, Support

        return SelectivePolicy(inner, Support.from_json(data["support"]))
    return inner


def policy_from_spec(spec: str | None) -> Policy | None:
    """``None``/"fixed" -> None (preset), "random:SEED", or a policy JSON path."""
    if not spec or spec == "fixed":
        return None
    if spec.startswith("random"):
        _, _, seed = spec.partition(":")
        return RandomPolicy(int(seed or 0))
    data = json.loads(Path(spec).read_text(encoding="utf-8"))
    if data.get("schema") == SET_SCHEMA:
        return PolicySet(
            {m: _policy_from_json(d) for m, d in data["modes"].items()}, data.get("status")
        )
    return _policy_from_json(data)
