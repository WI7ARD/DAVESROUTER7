"""Post-route optimisation (Stage 5.8-5.10, Stage 8 "tweak").

Deterministic and conservative: an optimisation only ever replaces a net's
*router-generated* copper (never source, locked or — by default — user-accepted
copper), and a change is kept only if

* the net stays connected (connectivity is re-checked on the result),
* every new segment/via passes the exact validator (the commit re-validates), and
* the selected goal metric strictly improves without the guard metrics getting
  worse (e.g. "fewer vias" may not make the route more than 25 % longer).

Otherwise the net is rolled back to its previous copper. Goals:

=================  =====================================  ===========================
goal               how                                    accepted when
=================  =====================================  ===========================
FEWER_VIAS         reroute with via cost x 8              vias down, length <= +25 %
SHORTER            reroute with bend/proximity cost 0     length down, vias not up
FEWER_BENDS        reroute with bend costs x 6            bends down, vias not up
MORE_CLEARANCE     reroute with proximity cost x 6        margin up, length <= +25 %
MERGE_COLLINEAR    join collinear generated segments      segment count down
=================  =====================================  ===========================
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.track import Track
from pcbrouter.geometry.clearance import gap_between
from pcbrouter.geometry.shapes import capsule
from pcbrouter.routing.connectivity import NetStatus, net_connectivity
from pcbrouter.routing.cost.model import DEFAULT_COST_MODEL, CostModel
from pcbrouter.routing.path.simplify import _direction
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, Provenance, WorkingBoard, stable_id
from pcbrouter.rules.model import ItemType

log = logging.getLogger(__name__)

LENGTH_GUARD = 1.25
MARGIN_PROBE_NM = 2_000_000


class OptimizeGoal(Enum):
    FEWER_VIAS = "fewer_vias"
    SHORTER = "shorter"
    FEWER_BENDS = "fewer_bends"
    MORE_CLEARANCE = "more_clearance"
    MERGE_COLLINEAR = "merge_collinear"


@dataclass(frozen=True, slots=True)
class NetMetrics:
    length_nm: float
    vias: int
    bends: int
    segments: int
    min_margin_nm: float | None  # smallest (gap - required clearance) to foreign copper

    def to_dict(self) -> dict[str, Any]:
        return {
            "length_mm": round(self.length_nm / 1e6, 4),
            "vias": self.vias,
            "bends": self.bends,
            "segments": self.segments,
            "min_margin_mm": (
                None if self.min_margin_nm is None else round(self.min_margin_nm / 1e6, 4)
            ),
        }


@dataclass
class OptimizeReport:
    goal: OptimizeGoal
    improved: dict[str, tuple[NetMetrics, NetMetrics]] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)


_GOAL_COST: dict[OptimizeGoal, CostModel] = {
    OptimizeGoal.FEWER_VIAS: replace(DEFAULT_COST_MODEL, via_nm=DEFAULT_COST_MODEL.via_nm * 8),
    OptimizeGoal.SHORTER: replace(
        DEFAULT_COST_MODEL, bend45_nm=0.0, bend90_nm=0.0, proximity_factor=0.0
    ),
    OptimizeGoal.FEWER_BENDS: replace(
        DEFAULT_COST_MODEL,
        bend45_nm=DEFAULT_COST_MODEL.bend45_nm * 6,
        bend90_nm=DEFAULT_COST_MODEL.bend90_nm * 6,
    ),
    OptimizeGoal.MORE_CLEARANCE: replace(DEFAULT_COST_MODEL, proximity_factor=1.8),
}


def net_metrics(wb: WorkingBoard, net: str) -> NetMetrics:
    board = wb.board
    tracks = [t for t in board.tracks if t.net_name == net]
    vias = [v for v in board.vias if v.net_name == net]
    ends: dict[tuple[str, int, int], list[tuple[int, int]]] = {}
    for t in tracks:
        d = _direction(t.start, t.end)
        for p, dd in ((t.start, d), (t.end, (-d[0], -d[1]))):
            ends.setdefault((t.layer, p.x, p.y), []).append(dd)
    bends = sum(1 for ds in ends.values() if len(ds) == 2 and ds[0] != (-ds[1][0], -ds[1][1]))
    return NetMetrics(
        sum(t.length for t in tracks), len(vias), bends, len(tracks), _margin(wb, tracks)
    )


def _margin(wb: WorkingBoard, tracks: list[Track]) -> float | None:
    engine = wb.engine
    geo = engine.geometry
    best: float | None = None
    for t in tracks:
        shape = capsule(t.start, t.end, t.width // 2)
        for item in geo.copper_near(t.layer, shape.bounds.expanded(MARGIN_PROBE_NM)):
            if item.net == t.net_name:
                continue
            req = engine.resolver.resolve_clearance(
                t.net_name, item.net, ItemType.TRACK, ItemType.PAD, t.layer
            ).value
            if req is None:
                continue
            m = min(gap_between(shape, s) for s in item.shapes) - req
            best = m if best is None else min(best, m)
    return best


def _better(goal: OptimizeGoal, a: NetMetrics, b: NetMetrics) -> bool:
    """Is ``b`` (after) better than ``a`` (before) for ``goal``?"""
    longer_ok = b.length_nm <= a.length_nm * LENGTH_GUARD
    if goal is OptimizeGoal.FEWER_VIAS:
        return b.vias < a.vias and longer_ok
    if goal is OptimizeGoal.SHORTER:
        return b.length_nm < a.length_nm - 1 and b.vias <= a.vias
    if goal is OptimizeGoal.FEWER_BENDS:
        return b.bends < a.bends and b.vias <= a.vias and longer_ok
    if goal is OptimizeGoal.MORE_CLEARANCE:
        if b.min_margin_nm is None or a.min_margin_nm is None:
            return False
        return b.min_margin_nm > a.min_margin_nm and longer_ok and b.vias <= a.vias
    return b.segments < a.segments


def _rippable(wb: WorkingBoard, net: str, allow_user: bool) -> list[str]:
    ok = {Provenance.ROUTER_GENERATED, Provenance.OPTIMIZER}
    if allow_user:
        ok.add(Provenance.USER_ACCEPTED)
    idx = wb.board.index
    out = []
    for obj_id in wb.generated_ids(net):
        obj = idx.tracks_by_id.get(obj_id) or idx.vias_by_id.get(obj_id)
        if obj is not None and wb.provenance_of(obj_id) in ok and not wb.is_locked(obj_id, net):
            out.append(obj_id)
    return out


def _connected(wb: WorkingBoard, net: str) -> bool:
    return net_connectivity(wb.engine.geometry, net).status in (
        NetStatus.FULLY_CONNECTED,
        NetStatus.NOT_APPLICABLE,
    )


def optimize_nets(
    wb: WorkingBoard,
    nets: list[str],
    goal: OptimizeGoal,
    *,
    control: Any = None,
    allow_user_accepted: bool = True,
    max_nets: int = 200,
    deadline: float | None = None,
) -> OptimizeReport:
    """Optimise ``nets`` in place on ``wb`` (use a fork for undoable batches).
    ``control`` (pause/cancel) and ``deadline`` (``time.perf_counter()`` value) are
    checked between nets and passed into every reroute search."""
    report = OptimizeReport(goal)
    cancel = getattr(control, "cancel_event", None)
    for net in sorted(nets)[:max_nets]:
        if control is not None and not control.checkpoint(deadline):
            report.log.append("optimisation cancelled")
            break
        if deadline is not None and time.perf_counter() > deadline:
            report.log.append("optimisation stopped: time budget reached")
            break
        ids = _rippable(wb, net, allow_user_accepted)
        if not ids:
            report.skipped[net] = "no router-generated copper to optimise"
            continue
        before = net_metrics(wb, net)
        was_connected = _connected(wb, net)
        mark = len(wb.commits)
        try:
            if goal is OptimizeGoal.MERGE_COLLINEAR:
                changed = _merge_collinear(wb, net, ids)
            else:
                changed = _reroute(wb, net, ids, goal, cancel, deadline)
        except CommitError as exc:
            changed = False
            report.log.append(f"{net}: {exc}")
        after = net_metrics(wb, net)
        keep = (
            changed and _better(goal, before, after) and (_connected(wb, net) or not was_connected)
        )
        if keep:
            report.improved[net] = (before, after)
            report.log.append(f"{net}: {goal.value} {before.to_dict()} -> {after.to_dict()}")
        else:
            while len(wb.commits) > mark:
                wb.undo()
            report.unchanged.append(net)
    log.info(
        "optimize.done goal=%s improved=%d unchanged=%d skipped=%d",
        goal.value,
        len(report.improved),
        len(report.unchanged),
        len(report.skipped),
    )
    return report


def _reroute(
    wb: WorkingBoard,
    net: str,
    ids: list[str],
    goal: OptimizeGoal,
    cancel: Any = None,
    deadline: float | None = None,
) -> bool:
    wb.commit_objects((), (), ids, f"optimize {net}: remove", Provenance.OPTIMIZER, validate=False)
    if deadline is not None:
        left = max(0.001, deadline - time.perf_counter())
        # One net must not consume the whole job budget: cap a single reroute.
        budget = min(left, 30.0)
    else:
        budget = 30.0
    req = RouteRequest(
        net,
        request_id=f"opt-{goal.value}-{net}",
        candidates=1,
        cost=_GOAL_COST[goal],
        time_limit_s=min(10.0, budget),
        total_time_limit_s=budget,
    )
    res = Router(wb.engine).route_net(req, cancel=cancel)
    if not res.candidates:
        return False
    best = res.candidates[0]
    wb.commit_proposals([best.proposal], f"optimize {net}: {goal.value}", Provenance.OPTIMIZER)
    return True


def _merge_collinear(wb: WorkingBoard, net: str, ids: list[str]) -> bool:
    idx = wb.board.index
    tracks = [idx.tracks_by_id[i] for i in ids if i in idx.tracks_by_id]
    vias = {(v.position.x, v.position.y) for v in wb.board.vias if v.net_name == net}
    degree: dict[tuple[str, int, int], int] = {}
    for t in (t for t in wb.board.tracks if t.net_name == net):
        for p in (t.start, t.end):
            degree[(t.layer, p.x, p.y)] = degree.get((t.layer, p.x, p.y), 0) + 1
    pool = {t.id: t for t in tracks}
    changed = True
    merged_any = False
    while changed:
        changed = False
        for a in sorted(pool.values(), key=lambda t: t.id):
            for b in sorted(pool.values(), key=lambda t: t.id):
                if a.id >= b.id or a.layer != b.layer or a.width != b.width:
                    continue
                shared = {(a.start.x, a.start.y), (a.end.x, a.end.y)} & {
                    (b.start.x, b.start.y),
                    (b.end.x, b.end.y),
                }
                if len(shared) != 1:
                    continue
                ((sx, sy),) = shared
                if degree.get((a.layer, sx, sy), 0) != 2 or (sx, sy) in vias:
                    continue
                pa = a.end if (a.start.x, a.start.y) == (sx, sy) else a.start
                pb = b.end if (b.start.x, b.start.y) == (sx, sy) else b.start
                joint = Point(sx, sy)
                if _direction(pa, joint) != _direction(joint, pb):
                    continue
                new = Track(stable_id("merge", a.id, b.id), pa, pb, a.width, a.layer, net)
                pool.pop(a.id)
                pool.pop(b.id)
                pool[new.id] = new
                changed = merged_any = True
                break
            if changed:
                break
    if not merged_any:
        return False
    old = set(ids) & set(idx.tracks_by_id)
    keep_new = [t for t in pool.values() if t.id not in old]
    removed = [i for i in old if i not in pool]
    wb.commit_objects(
        keep_new, (), removed, f"optimize {net}: merge collinear", Provenance.OPTIMIZER
    )
    return True
