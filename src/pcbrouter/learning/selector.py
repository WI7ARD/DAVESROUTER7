"""Learning: decide per board whether a learned policy may route it.

::

    board profile ──► like the training boards? ──no──► FALLBACK_FIXED (reason)
                            │ yes
                            ▼
              similar boards measured better  ──no──► FALLBACK_FIXED (reason)
              under this policy (A/B evidence)?
                            │ yes
                            ▼
                  LEARNED: the policy picks search settings per net

The fixed router is always the fallback, and every decision says why. The
learned policy only ever chooses among safe search variants; the exact
validator still decides whether any copper is legal.

**Why board-level evidence.** The policy is learned per net ("did this attempt
route this net?"), which cannot see what a net's copper does to the nets routed
after it. On ``medium_4layer`` a greedier first attempt routed its own net but
blocked four later ones (NO_PATH after a few hundred expansions). Only a
board-level A/B measures that, so the selector asks for such measurements on
similar boards before trusting the policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pcbrouter.learning.features import DISTANCE_FEATURES, PROFILE_VERSION, transform
from pcbrouter.learning.policy import Policy, ThompsonPolicy

SUPPORT_SCHEMA = "pcbrouter-policy-support/1"
LEARNED = "LEARNED"
FALLBACK = "FALLBACK_FIXED"


@dataclass
class SupportBoard:
    """One training board: its standardized profile and, when measured, the
    policy-minus-fixed board result per mode ({"speed": {"delta": ..., "runs": n}})."""

    z: list[float]
    evidence: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class Support:
    """Where a policy was trained, and how far from it we still trust it."""

    center: list[float]
    scale: list[float]
    boards: list[SupportBoard]
    #: nearest-training-board distance above which a board is out of distribution
    radius: float
    #: similar boards (within ``radius``) consulted for evidence
    k: int = 3
    min_similar: int = 1
    require_evidence: bool = True
    features: list[str] = field(default_factory=lambda: list(DISTANCE_FEATURES))
    #: raw per-feature range of the training boards (for readable reasons)
    lo: list[float] = field(default_factory=list)
    hi: list[float] = field(default_factory=list)
    profile_version: int = PROFILE_VERSION

    def standardize(self, profile: dict[str, float]) -> np.ndarray:
        out: np.ndarray = (transform(profile) - np.array(self.center)) / np.array(self.scale)
        return out

    # ------------------------------------------------------------ persistence
    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SUPPORT_SCHEMA,
            "profile_version": self.profile_version,
            "features": self.features,
            "center": self.center,
            "scale": self.scale,
            "radius": self.radius,
            "k": self.k,
            "min_similar": self.min_similar,
            "require_evidence": self.require_evidence,
            "lo": self.lo,
            "hi": self.hi,
            "boards": [{"z": b.z, "evidence": b.evidence} for b in self.boards],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Support:
        if d.get("schema") != SUPPORT_SCHEMA:
            raise ValueError(f"not a policy support block: {d.get('schema')!r}")
        if d.get("profile_version") != PROFILE_VERSION or d.get("features") != list(
            DISTANCE_FEATURES
        ):
            raise ValueError("policy support was built with other board features; retrain")
        return cls(
            center=[float(x) for x in d["center"]],
            scale=[float(x) for x in d["scale"]],
            boards=[
                SupportBoard([float(x) for x in b["z"]], b.get("evidence") or {})
                for b in d["boards"]
            ],
            radius=float(d["radius"]),
            k=int(d.get("k", 3)),
            min_similar=int(d.get("min_similar", 1)),
            require_evidence=bool(d.get("require_evidence", True)),
            lo=[float(x) for x in d.get("lo", [])],
            hi=[float(x) for x in d.get("hi", [])],
        )


def fit_support(
    profiles: Sequence[dict[str, float]],
    *,
    quantile: float = 0.75,
    k: int = 3,
    min_similar: int = 1,
    require_evidence: bool = True,
) -> Support:
    """Support from training-board profiles. The out-of-distribution radius is
    the *quantile* of the training boards' own nearest-neighbour distances: a
    new board farther from every training board than most training boards are
    from each other is not covered (0.75 = conservative)."""
    if not profiles:
        raise ValueError("no training boards")
    x = np.array([transform(p) for p in profiles])
    center = x.mean(axis=0)
    scale = np.maximum(x.std(axis=0), 0.1)  # floor: a feature that never varied
    z = (x - center) / scale
    if len(z) >= 2:
        d = np.sqrt(((z[:, None, :] - z[None, :, :]) ** 2).sum(-1))
        np.fill_diagonal(d, np.inf)
        radius = float(np.quantile(d.min(axis=1), quantile))
    else:
        radius = 0.0
    raw = np.array([[p.get(f, 0.0) for f in DISTANCE_FEATURES] for p in profiles])
    return Support(
        center=center.tolist(),
        scale=scale.tolist(),
        boards=[SupportBoard(row.tolist()) for row in z],
        radius=radius,
        k=k,
        min_similar=min_similar,
        require_evidence=require_evidence,
        lo=raw.min(axis=0).tolist(),
        hi=raw.max(axis=0).tolist(),
    )


def _unlike(support: Support, profile: dict[str, float], z: np.ndarray, near: int) -> str:
    """The features that set this board apart from its nearest training board."""
    diff = np.abs(z - np.array(support.boards[near].z))
    names = list(DISTANCE_FEATURES)
    parts = []
    for i in np.argsort(-diff)[:3]:
        f = names[i]
        rng = ""
        if support.lo and support.hi:
            rng = f" (training {support.lo[i]:.3g}-{support.hi[i]:.3g})"
        parts.append(f"{f}={profile.get(f, 0.0):.3g}{rng}")
    return ", ".join(parts)


class SelectivePolicy(Policy):
    """A learned policy plus the rule for when to use it (see module docstring)."""

    name = "selective"

    def __init__(self, inner: ThompsonPolicy, support: Support, mode: str | None = None):
        self.inner = inner
        self.support = support
        modes = inner.trained_modes or ()
        self.mode = mode or (modes[0] if len(modes) == 1 else None)

    def policy_id(self) -> str:
        return self.inner.policy_id()

    def choose(self, ctx: str, net: str, attempt: int) -> str:
        return self.inner.choose(ctx, net, attempt)

    def compatibility(self) -> str | None:
        return self.inner.compatibility()

    def select(self, profile: dict[str, float]) -> tuple[Policy | None, dict[str, Any]]:
        s = self.support
        base: dict[str, Any] = {"policy_id": self.policy_id(), "radius": round(s.radius, 3)}
        if not s.boards:
            return None, {**base, "decision": FALLBACK, "reason": "policy has no training boards"}
        z = s.standardize(profile)
        dist = np.sqrt(((np.array([b.z for b in s.boards]) - z) ** 2).sum(axis=1))
        order = np.argsort(dist)
        nn = float(dist[order[0]])
        base["nearest_distance"] = round(nn, 3)
        similar = [int(i) for i in order if dist[i] <= s.radius][: s.k]
        base["similar_boards"] = len(similar)
        if nn > s.radius or len(similar) < s.min_similar:
            why = (
                f"out of distribution: nearest training board at distance {nn:.2f} "
                f"(limit {s.radius:.2f}); most unlike on {_unlike(s, profile, z, int(order[0]))}"
            )
            return None, {**base, "decision": FALLBACK, "reason": why, "text": f"{FALLBACK}: {why}"}
        if s.require_evidence:
            mode = self.mode or ""
            ev = [s.boards[i].evidence.get(mode) for i in similar]
            measured = [e for e in ev if e]
            base["evidence"] = [
                {"distance": round(float(dist[i]), 3), **(e or {})}
                for i, e in zip(similar, ev, strict=True)
            ]
            if not measured:
                why = f"no board-level A/B evidence for {mode or 'this mode'} on similar boards"
                return None, {**base, "decision": FALLBACK, "reason": why,
                              "text": f"{FALLBACK}: {why}"}  # fmt: skip
            worst = min(float(e["delta"]) for e in measured)
            total = sum(float(e["delta"]) for e in measured)
            if worst < 0 or total <= 0:
                why = (
                    f"similar boards did not gain under this policy "
                    f"(net deltas {', '.join(f'{float(e['delta']):+g}' for e in measured)})"
                )
                return None, {**base, "decision": FALLBACK, "reason": why,
                              "text": f"{FALLBACK}: {why}"}  # fmt: skip
        conf = max(0.0, 1.0 - nn / s.radius) if s.radius > 0 else 0.0
        base["confidence"] = round(conf, 3)
        text = (
            f"{LEARNED} ({self.policy_id()}): nearest training board at {nn:.2f} "
            f"(limit {s.radius:.2f}), {len(similar)} similar board(s)"
        )
        return self.inner, {**base, "decision": LEARNED, "policy": self.inner.name, "text": text}
