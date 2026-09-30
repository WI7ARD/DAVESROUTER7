"""Full-board routing (Stage 5): plan, ordered passes, rip-up/reroute, metrics.

The job runs on a *fork* of the working board (never the live one) so the UI stays
responsive and nothing changes until the user accepts the batch — or individual
nets of it. Passes::

    1  route every task in plan order (auto-accepting the best validated candidate
       into the fork)
    2  retry failures with congestion feedback and larger search limits
    3  bounded rip-up/reroute: for a failed net, remove interfering *router
       generated* routes (never source, locked or user-accepted copper unless
       allowed), route the net, then reroute the displaced nets; the change is
       kept only if the number of routed nets does not decrease
    4  optional optimisation (via reduction / length) — see ``optimize.py``

Every piece of copper committed to the fork passed the exact validator (the fork
re-validates each commit). Limits: passes, rip-ups per net, total rip-ups, per-net
search limits, wall-clock budget, pause/resume/cancel.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.board import Board
from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.track import Track
from pcbrouter.domain.via import Via
from pcbrouter.routing.connectivity import NetStatus, net_connectivity
from pcbrouter.routing.occupancy import GridSpec
from pcbrouter.routing.request import DEFAULT_GRID_NM, RouteRequest, with_user_constraints
from pcbrouter.routing.result import FailureReason, RouteResult, RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, Provenance, WorkingBoard

log = logging.getLogger(__name__)

DEFAULT_MAX_PASSES = 3
DEFAULT_MAX_RIPUPS_PER_NET = 2
DEFAULT_MAX_TOTAL_RIPUPS = 20
DEFAULT_BUDGET_S = 600.0
CONGESTION_TILE_CELLS = 10  # congestion field resolution for feedback (cells per tile)
#: finest grid a silent NO_PATH retry may drop to (nm); deeper refinement is an
#: explicit user choice (Speed/Accuracy presets, workbench constraints)
FINE_GRID_FLOOR_NM = 25_000


#: Speed's fine-grid retry only follows a failure this small (see route_net_refined)
REFINE_MAX_NODES = 20_000


def route_net_refined(
    router: Any, req: RouteRequest, cancel: Any = None, penalties: Any = None
) -> RouteResult:
    """``router.route_net``; if the search proves there is no path (or no escape)
    on a grid coarser than the default 0.1 mm — Speed's 0.2 mm grid — try once more
    on the default grid. Fine-pitch pads (e.g. 0.5 mm QFP rows) often have no free
    cell on a 0.2 mm grid even on an empty board, while the default grid escapes
    them. One step only; Accuracy (default grid) is unaffected.

    Only a *small* failure is retried (at most REFINE_MAX_NODES expanded): that is
    the boxed-in pad the retry exists for. A NO_PATH proven after a large search is
    congestion, which a finer grid does not fix but pays for (measured on
    kit-dev-coldfire Speed, 1 worker, 600 s: 121/209 with an unconditional retry,
    134/209 without)."""
    res: RouteResult = router.route_net(req, cancel=cancel, penalties=penalties)
    if (
        res.status is RouteStatus.SUCCESS
        or res.reason not in (FailureReason.NO_PATH, FailureReason.NO_ESCAPE)
        or req.grid_resolution <= DEFAULT_GRID_NM
        or res.metrics.expanded_nodes > REFINE_MAX_NODES
        or (cancel is not None and cancel.is_set())
    ):
        return res
    finer = replace(
        req,
        grid_resolution=max(DEFAULT_GRID_NM, req.grid_resolution // 2),
        request_id=f"{req.request_id}-fine",
    )
    first = res.metrics
    res = router.route_net(finer, cancel=cancel, penalties=penalties)
    m = res.metrics
    for name in ("expanded_nodes", "searches", "repairs", "elapsed_s", "grid_s", "search_s",
                 "geometry_s", "validate_s"):  # fmt: skip
        setattr(m, name, getattr(m, name) + getattr(first, name))
    return res


#: pass 1 tries EVERY net first within a fair share of the budget: at most this
#: many nodes per search and PASS1_SLICE x (budget / nets) seconds per net
#: (>= PASS1_MIN_SLICE_S). Unfinished nets continue in pass 2. Measured on a
#: 4-layer board: without it GND/+3.3V (78/88 connections) took 230 s/180 s and
#: the budget ran out after 25 of 209 nets.
PASS1_NODE_LIMIT = 300_000
PASS1_SLICE = 3.0
PASS1_MIN_SLICE_S = 2.0
#: pass 2+ and rip-up: one net may use at most this share of the board budget
#: (>= PASS2_MIN_SLICE_S). Measured on a 4-layer ESC: a board-wide GND pour took
#: 600 s of a 900 s budget in pass 2 (and still failed), so rip-up never ran for
#: the three nets that only needed earlier copper moved.
PASS2_SHARE = 0.2
PASS2_MIN_SLICE_S = 30.0


class Strategy(Enum):
    MOST_CONSTRAINED = "most_constrained"  # fewest escape options / most pads first
    SHORTEST_FIRST = "shortest_first"
    CRITICAL_FIRST = "critical_first"  # user/AI priorities, diff pairs, power, then short
    FEWEST_ESCAPES = "fewest_escapes"
    CONGESTION_AWARE = "congestion_aware"  # nets through congested regions first


class TaskKind(Enum):
    SIGNAL = "signal"
    POWER = "power"
    DIFF_PAIR = "diff_pair"
    GROUP = "group"


@dataclass(frozen=True, slots=True)
class RouteTask:
    net: str
    kind: TaskKind = TaskKind.SIGNAL
    priority: int = 0  # higher first (explicit user/AI priority)
    group: str | None = None  # RouteGroup name / diff-pair partner
    request: RouteRequest | None = None  # per-task overrides (width, layers, vias...)
    # ordering features (filled by make_plan)
    pads: int = 0
    airwire_length: float = 0.0
    escape_options: int = 99
    congestion: float = 0.0


@dataclass(frozen=True, slots=True)
class RouteGroup:
    """A bus/group routed together (contiguous in the plan, shared constraints)."""

    name: str
    nets: tuple[str, ...]
    priority: int = 0
    request: RouteRequest | None = None


@dataclass
class BoardRoutingPlan:
    tasks: list[RouteTask]
    strategy: Strategy
    notes: list[str] = field(default_factory=list)

    @property
    def nets(self) -> list[str]:
        return [t.net for t in self.tasks]


def _failure_text(res: RouteResult) -> str:
    """One line for the Routing Jobs panel: reason, where, and how hard it tried."""
    at = res.failed_at or {}
    where = f" between {at['start']} and {at['goal']} mm" if "start" in at else ""
    return (
        f"{res.message or 'no route'}{where} "
        f"[{res.metrics.expanded_nodes:,} nodes, {res.metrics.elapsed_s:.1f} s]"
    )


@dataclass
class BoardRouterSettings:
    strategy: Strategy = Strategy.CONGESTION_AWARE
    max_passes: int = DEFAULT_MAX_PASSES
    allow_ripup: bool = True
    ripup_user_accepted: bool = False
    #: rip-up may only remove copper this job created: every route that was on
    #: the board when the job started stays exactly where it is
    preserve_existing: bool = False
    max_ripups_per_net: int = DEFAULT_MAX_RIPUPS_PER_NET
    max_total_ripups: int = DEFAULT_MAX_TOTAL_RIPUPS
    budget_s: float = DEFAULT_BUDGET_S
    base_request: RouteRequest = field(default_factory=lambda: RouteRequest("", candidates=1))
    optimize: bool = False
    priorities: dict[str, int] = field(default_factory=dict)
    groups: tuple[RouteGroup, ...] = ()
    #: explicit differential pairs (positive, negative) in addition to the ones
    #: detected by name; a pair is routed consecutively with a soft corridor
    pairs: tuple[tuple[str, str], ...] = ()
    #: helper processes for parallel routing (see routing/parallel.py); 0/1 = off.
    #: The caller only sets it for the CPU backend.
    parallel_workers: int = 0


class BoardRoutingControl:
    """Pause / resume / cancel for a running job (thread-safe)."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._running = threading.Event()
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> None:
        self.cancel_event.set()
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def checkpoint(self, deadline: float | None = None) -> bool:
        """Block while paused; return False when cancelled or past ``deadline``."""
        while not self._running.wait(0.1):
            if self.cancel_event.is_set():
                return False
            if deadline is not None and time.perf_counter() > deadline:
                return False
        if deadline is not None and time.perf_counter() > deadline:
            return False
        return not self.cancel_event.is_set()


class BoardStatus(Enum):
    FULLY_ROUTED = "FULLY_ROUTED"
    PARTIALLY_ROUTED = "PARTIALLY_ROUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NOTHING_TO_ROUTE = "NOTHING_TO_ROUTE"


@dataclass
class NetOutcome:
    net: str
    status: RouteStatus
    reason: FailureReason | None = None
    message: str = ""
    length_nm: float = 0.0
    vias: int = 0
    passes: int = 0
    added_ids: list[str] = field(default_factory=list)
    #: generated ids this net's final route required removing (rip-up)
    removed_ids: list[str] = field(default_factory=list)
    #: effort over the whole job (every pass): searches started for this net,
    #: states expanded and seconds spent (learning / experience log)
    attempts: int = 0
    expanded_nodes: int = 0
    route_s: float = 0.0


@dataclass
class BoardMetrics:
    nets_attempted: int = 0
    nets_completed: int = 0
    nets_failed: int = 0
    total_length_nm: float = 0.0
    new_vias: int = 0
    runtime_s: float = 0.0
    expanded_nodes: int = 0
    ripups: int = 0
    reroutes: int = 0
    passes: int = 0
    #: completed nets routed with zero exact-validation repairs (first-try clean)
    clean_nets: int = 0
    #: parallel routing: nets routed by helpers, and helper results that were no
    #: longer legal when they arrived (re-queued / rerouted)
    parallel_batches: int = 0
    parallel_conflicts: int = 0
    #: summed RouteMetrics phase seconds (see RouteMetrics for the split)
    grid_s: float = 0.0
    search_s: float = 0.0
    geometry_s: float = 0.0
    validate_s: float = 0.0

    @property
    def completion(self) -> float:
        return self.nets_completed / self.nets_attempted if self.nets_attempted else 1.0

    @property
    def clean_rate(self) -> float:
        """Share of completed nets needing no validation repair."""
        return self.clean_nets / self.nets_completed if self.nets_completed else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "nets_attempted": self.nets_attempted,
            "nets_completed": self.nets_completed,
            "nets_failed": self.nets_failed,
            "completion_pct": round(100 * self.completion, 1),
            "total_routed_length_mm": round(self.total_length_nm / 1e6, 3),
            "new_vias": self.new_vias,
            "runtime_s": round(self.runtime_s, 2),
            "expanded_nodes": self.expanded_nodes,
            "ripups": self.ripups,
            "reroutes": self.reroutes,
            "passes": self.passes,
            "clean_nets": self.clean_nets,
            "clean_rate_pct": round(100 * self.clean_rate, 1),
            "parallel_batches": self.parallel_batches,
            "parallel_conflicts": self.parallel_conflicts,
            "grid_s": round(self.grid_s, 2),
            "search_s": round(self.search_s, 2),
            "geometry_s": round(self.geometry_s, 2),
            "validate_s": round(self.validate_s, 2),
        }

    def absorb(self, metrics: Any) -> None:
        """Add one net's RouteMetrics phase seconds and node count."""
        self.expanded_nodes += metrics.expanded_nodes
        self.grid_s += metrics.grid_s
        self.search_s += metrics.search_s
        self.geometry_s += metrics.geometry_s
        self.validate_s += metrics.validate_s


@dataclass
class BoardRoutingResult:
    status: BoardStatus
    base_board: Board
    final_board: Board
    plan: BoardRoutingPlan
    outcomes: dict[str, NetOutcome]
    metrics: BoardMetrics
    added_tracks: tuple[Track, ...] = ()
    added_vias: tuple[Via, ...] = ()
    removed_ids: tuple[str, ...] = ()
    log: list[str] = field(default_factory=list)
    #: removed base object id -> its net: a removal belongs to the change set of
    #: the net that owned the copper (rerouting X = "remove old X, add new X")
    removed_nets: dict[str, str | None] = field(default_factory=dict)
    #: net -> nets whose removed copper its new copper needs gone (see
    #: :func:`copper_dependencies`); accepting a net accepts its closure
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def summary(self) -> str:
        m = self.metrics
        return (
            f"ROUTER RESULT board: {self.status.value} — {m.nets_completed}/{m.nets_attempted} "
            f"nets ({100 * m.completion:.0f} %), {m.new_vias} new via(s), "
            f"{m.total_length_nm / 1e6:.1f} mm, {m.ripups} rip-up(s), {m.runtime_s:.1f} s"
        )

    def dependency_closure(self, nets: set[str]) -> set[str]:
        """``nets`` plus every net they transitively depend on: accepting only part
        of a rip-up/reroute would remove a route without its replacement (or add
        copper where old copper still sits), so dependents are accepted together."""
        out: set[str] = set()
        todo = list(nets)
        while todo:
            net = todo.pop()
            if net in out:
                continue
            out.add(net)
            todo.extend(self.dependencies.get(net, ()))
        return out

    def required_extra_nets(self, nets: set[str]) -> set[str]:
        """Nets that accepting ``nets`` pulls in (for the approval text)."""
        return self.dependency_closure(nets) - set(nets)

    def objects_for(self, nets: set[str] | None = None) -> tuple[list[Track], list[Via], list[str]]:
        """Copper to add and ids to remove for accepting ``nets`` (all when None).

        Dependency-safe: the selection is widened to its dependency closure, and a
        removal is applied only together with its own net's replacement copper —
        never "Y routed, X's route removed, X not re-added"."""
        if nets is None:
            return list(self.added_tracks), list(self.added_vias), list(self.removed_ids)
        closure = self.dependency_closure(set(nets))
        tracks = [t for t in self.added_tracks if t.net_name in closure]
        vias = [v for v in self.added_vias if v.net_name in closure]
        removed = [i for i in self.removed_ids if self.removed_nets.get(i) in closure]
        return tracks, vias, removed


#: added copper within this distance of removed copper of another net depends
#: on that removal (generous: larger than any clearance on the tested boards;
#: a wider net only groups more nets together, never fewer)
DEPENDENCY_MARGIN_NM = 1_000_000


def _removed_nets(base: Board, removed: Iterable[str]) -> dict[str, str | None]:
    idx = base.index
    out: dict[str, str | None] = {}
    for i in removed:
        obj = idx.tracks_by_id.get(i) or idx.vias_by_id.get(i)
        out[i] = obj.net_name if obj is not None else None
    return out


def copper_dependencies(
    base: Board,
    added_tracks: Iterable[Track],
    added_vias: Iterable[Via],
    removed_nets: dict[str, str | None],
) -> dict[str, tuple[str, ...]]:
    """net A -> nets B whose removed copper lies within DEPENDENCY_MARGIN_NM of A's
    added copper: A's route may occupy space B's old route held, so A can only be
    accepted together with B's change set (B's removal and B's replacement)."""
    idx = base.index
    removed_boxes: list[tuple[str, BoundingBox]] = []
    for i, net in removed_nets.items():
        obj = idx.tracks_by_id.get(i) or idx.vias_by_id.get(i)
        if obj is not None and net is not None:
            removed_boxes.append((net, obj.bounds.expanded(DEPENDENCY_MARGIN_NM)))
    deps: dict[str, set[str]] = {}
    if not removed_boxes:
        return {}
    added: list[Track | Via] = [*added_tracks, *added_vias]
    for item in added:
        net = item.net_name
        if net is None:
            continue
        box = item.bounds
        for other, rbox in removed_boxes:
            if other != net and rbox.intersects(box):
                deps.setdefault(net, set()).add(other)
    return {k: tuple(sorted(v)) for k, v in sorted(deps.items())}


# ---------------------------------------------------------------- planning
_DIFF_SUFFIXES = (("_P", "_N"), ("+", "-"), ("P", "N"), ("_DP", "_DN"))
_POWER_RE = re.compile(r"^(\+?\d+V\d*|V(CC|DD|BUS|BAT|IN|SYS)\w*|PWR\w*|\+\w+)$", re.I)


def diff_pairs(nets: list[str]) -> list[tuple[str, str]]:
    names = set(nets)
    pairs: list[tuple[str, str]] = []
    for net in sorted(names):
        for sp, sn in _DIFF_SUFFIXES:
            if net.endswith(sp):
                other = net[: -len(sp)] + sn
                if other in names and len(net) > len(sp):
                    pairs.append((net, other))
                    break
    return pairs


def _task_features(wb: WorkingBoard, net: str) -> tuple[int, float, int, float]:
    engine = wb.engine
    conn = net_connectivity(engine.geometry, net)
    length = sum(a.length for a in conn.airwires)
    escapes = 99
    for uid in conn.pad_uids[:4]:  # bounded: escape analysis is 8 checks per layer
        try:
            esc = engine.pin_escape(uid)
        except (KeyError, ValueError):
            continue
        free = sum(len(esc.free_directions(layer)) for layer in esc.directions)
        escapes = min(escapes, free)
    congestion = 0.0
    try:  # R5: worst copper density near this net's pads (0..1), advisory only
        geo = engine.geometry
        for uid in conn.pad_uids[:8]:
            item = geo.copper.get(uid)
            if item is None:
                continue
            for layer in sorted(item.layers):
                value = engine.congestion(layer).value_at(item.bounds.center)
                if value is not None:
                    congestion = max(congestion, value)
    except Exception:
        log.debug("task congestion unavailable for %s", net, exc_info=True)
    return len(conn.pad_uids), length, escapes, congestion


def make_plan(
    wb: WorkingBoard,
    settings: BoardRouterSettings,
    nets: list[str] | None = None,
) -> BoardRoutingPlan:
    """Deterministic plan of incomplete nets. AI/user priorities come from
    ``settings.priorities``; they only reorder, they cannot add illegal work."""
    engine = wb.engine
    conn = engine.connectivity
    candidates = [
        n
        for n, c in sorted(conn.nets.items())
        if c.status in (NetStatus.UNROUTED, NetStatus.PARTIALLY_CONNECTED)
        and (nets is None or n in nets)
        and not wb.is_locked("", n)
    ]
    notes: list[str] = []
    pairs = diff_pairs(candidates)
    paired = {n for pair in pairs for n in pair}
    for a, b in settings.pairs:
        if a in candidates and b in candidates and not {a, b} & paired:
            pairs.append((a, b))
            paired |= {a, b}
    partner = {a: b for a, b in pairs} | {b: a for a, b in pairs}
    group_of = {n: g.name for g in settings.groups for n in g.nets}
    group_prio = {g.name: g.priority for g in settings.groups}
    resolver = engine.resolver
    default_w = resolver.resolve_trace_width(None).value or 0
    tasks: list[RouteTask] = []
    for net in candidates:
        pads, length, escapes, congestion = _task_features(wb, net)
        width = resolver.resolve_trace_width(net).value or 0
        if net in partner:
            kind = TaskKind.DIFF_PAIR
        elif width > default_w or _POWER_RE.match(net.lstrip("/")):
            kind = TaskKind.POWER
        elif net in group_of:
            kind = TaskKind.GROUP
        else:
            kind = TaskKind.SIGNAL
        prio = settings.priorities.get(net, group_prio.get(group_of.get(net) or "", 0))
        user = wb.net_constraints.get(net)
        req = with_user_constraints(replace(settings.base_request, net=net), user) if user else None
        user_prio = user.get("priority") if user else None
        if isinstance(user_prio, int):
            prio = max(prio, user_prio)
        tasks.append(
            RouteTask(
                net,
                kind,
                prio,
                partner.get(net) or group_of.get(net),
                req,
                pads,
                length,
                escapes,
                congestion,
            )
        )
    if pairs:
        notes.append(
            f"{len(pairs)} differential pair(s): " + ", ".join(f"{a}/{b}" for a, b in pairs)
        )
    return BoardRoutingPlan(order_tasks(tasks, settings.strategy), settings.strategy, notes)


def order_tasks(tasks: list[RouteTask], strategy: Strategy) -> list[RouteTask]:
    kind_rank = {TaskKind.DIFF_PAIR: 0, TaskKind.POWER: 1, TaskKind.GROUP: 2, TaskKind.SIGNAL: 3}

    def key(t: RouteTask) -> tuple[Any, ...]:
        if strategy is Strategy.SHORTEST_FIRST:
            return (-t.priority, t.airwire_length, t.net)
        if strategy is Strategy.MOST_CONSTRAINED:
            return (-t.priority, t.escape_options, -t.pads, t.airwire_length, t.net)
        if strategy is Strategy.FEWEST_ESCAPES:
            return (-t.priority, t.escape_options, t.net)
        if strategy is Strategy.CONGESTION_AWARE:
            return (-t.priority, -t.congestion, t.airwire_length, t.net)
        return (-t.priority, kind_rank[t.kind], t.airwire_length, t.net)

    ordered = sorted(tasks, key=key)
    # keep diff-pair partners and groups contiguous (first member's position wins)
    out: list[RouteTask] = []
    placed: set[str] = set()
    by_group: dict[str, list[RouteTask]] = {}

    def group_key(t: RouteTask) -> str | None:
        if not t.group:
            return None
        return "|".join(sorted([t.net, t.group])) if t.kind is TaskKind.DIFF_PAIR else t.group

    for t in ordered:
        gk = group_key(t)
        if gk is not None:
            by_group.setdefault(gk, []).append(t)
    for t in ordered:
        if t.net in placed:
            continue
        gk = group_key(t)
        members = by_group.get(gk, [t]) if gk is not None else [t]
        for m in members:
            if m.net not in placed:
                out.append(m)
                placed.add(m.net)
    return out


# ---------------------------------------------------------------- execution
Progress = Callable[[dict[str, Any]], None]


class BoardRouter:
    #: compiled-grid inputs kept across passes/rip-ups (R2); occupancy work is
    #: the expensive part, entries are bounded below.
    GRID_CACHE_SIZE = 4

    def __init__(
        self,
        working: WorkingBoard,
        settings: BoardRouterSettings | None = None,
        router_factory: Callable[[Any], Router] | None = None,
    ) -> None:
        self.base = working
        self.settings = settings or BoardRouterSettings()
        inner = router_factory or (lambda engine: Router(engine))
        self._grid_cache: dict[Any, Any] = {}

        def factory(engine: Any) -> Router:
            router = inner(engine)
            router.grid_cache = self._grid_cache
            if len(self._grid_cache) > self.GRID_CACHE_SIZE:
                self._grid_cache.pop(next(iter(self._grid_cache)))
            return router

        self.router_factory = factory
        self._all_tasks: list[RouteTask] = []
        self._base_ids: set[str] = set()
        self._ripup_tries: dict[str, int] = {}
        self._outcomes: dict[str, NetOutcome] = {}
        self._deadline: float | None = None
        self._emit: Callable[..., None] = lambda **_kw: None

    def run(
        self,
        plan: BoardRoutingPlan | None = None,
        control: BoardRoutingControl | None = None,
        progress: Progress | None = None,
        on_partial: Callable[[Any], None] | None = None,
    ) -> BoardRoutingResult:
        t0 = time.perf_counter()
        control = control or BoardRoutingControl()
        s = self.settings
        fork = self.base.fork()
        base_board = fork.board
        base_ids = {t.id for t in base_board.tracks} | {v.id for v in base_board.vias}
        self._base_ids = base_ids
        #: rip-up attempts per net over the WHOLE job (every pass), so
        #: max_ripups_per_net is a job limit, not a per-pass one
        self._ripup_tries = {}
        plan = plan or make_plan(fork, s)
        self._all_tasks = list(plan.tasks)
        metrics = BoardMetrics(nets_attempted=len(plan.tasks))
        outcomes: dict[str, NetOutcome] = {
            t.net: NetOutcome(t.net, RouteStatus.NO_ROUTE) for t in plan.tasks
        }
        self._outcomes = outcomes
        job_log: list[str] = []
        deadline = t0 + s.budget_s
        self._deadline = deadline
        cancelled = False
        out_of_time = False  # the budget ran out: not a user cancel
        last_partial = 0.0

        def emit_partial(force: bool = False) -> None:
            """Stream what is routed so far (live preview + cancel keeps it)."""
            nonlocal last_partial
            if on_partial is None:
                return
            now = time.perf_counter()
            if not force and now - last_partial < 2.0:
                return
            last_partial = now
            final = fork.board
            added_t = tuple(t for t in final.tracks if t.id not in base_ids)
            added_v = tuple(v for v in final.vias if v.id not in base_ids)
            final_ids = {t.id for t in final.tracks} | {v.id for v in final.vias}
            done = {
                net: (
                    o.status.value,
                    o.reason.value if o.reason else "",
                    o.message,
                    list(o.added_ids),
                )
                for net, o in outcomes.items()
                if o.passes > 0 or o.status is RouteStatus.SUCCESS or o.added_ids
            }
            succeeded = sum(1 for o in outcomes.values() if o.status is RouteStatus.SUCCESS)
            from pcbrouter.jobs.protocol import BoardPartial

            removed_p = sorted(base_ids - final_ids)
            removed_nets_p = _removed_nets(base_board, removed_p)
            on_partial(
                BoardPartial(
                    completed_nets=len(done),
                    total_nets=len(plan.tasks),
                    succeeded_nets=succeeded,
                    outcomes=done,
                    added_tracks=list(added_t),
                    added_vias=list(added_v),
                    removed_ids=removed_p,
                    removed_nets=removed_nets_p,
                    dependencies=copper_dependencies(base_board, added_t, added_v, removed_nets_p),
                )
            )

        def emit(**kw: Any) -> None:
            if progress is not None:
                progress({"metrics": metrics.to_dict(), "total_passes": s.max_passes, **kw})

        self._emit = emit
        emit(state="planning", phase="PLANNING", total_nets=len(plan.tasks))

        failed = [t for t in plan.tasks]
        par = self._start_parallel(fork, plan, job_log)
        try:
            for pass_no in range(1, max(1, s.max_passes) + 1):
                if not failed:
                    break
                metrics.passes = pass_no
                still: list[RouteTask] = []
                sequential = failed
                if par is not None and len(failed) >= 2:
                    stop = self._parallel_pass(
                        fork, failed, still, pass_no, outcomes, metrics, control, job_log,
                        par, emit, emit_partial, len(plan.tasks),
                    )  # fmt: skip
                    cancelled = stop == "cancelled"
                    out_of_time = stop == "out_of_time"
                    sequential = []
                for i, task in enumerate(sequential):
                    if not control.checkpoint(deadline) or time.perf_counter() > deadline:
                        if control.cancel_event.is_set():
                            cancelled = True
                        else:
                            out_of_time = True
                        break
                    emit(
                        net=task.net,
                        index=i + 1,
                        total=len(failed),
                        pass_no=pass_no,
                        state="routing",
                        phase="ROUTING",
                        total_nets=len(plan.tasks),
                        completed_nets=sum(
                            1 for o in outcomes.values() if o.status is RouteStatus.SUCCESS
                        ),
                    )
                    ok = self._route_task(fork, task, pass_no, outcomes, metrics, control, job_log)
                    if not ok:
                        still.append(task)
                    emit_partial()
                if cancelled or out_of_time:
                    emit_partial(force=True)
                    break
                failed = still
                if pass_no == 1 and failed and s.max_passes >= 2:
                    job_log.append(
                        f"pass 1: {len(failed)} net(s) failed; retrying with congestion feedback"
                    )
                if pass_no >= 2 and failed and s.allow_ripup:
                    failed = self._ripup_pass(
                        fork, failed, outcomes, metrics, control, job_log, deadline
                    )
                    emit_partial()
        finally:
            if par is not None:
                par.close()
        emit_partial(force=True)  # latest snapshot always matches the result below
        if s.optimize and not cancelled and not out_of_time:
            from pcbrouter.routing.optimize import OptimizeGoal, optimize_nets

            done_nets = [n for n, o in outcomes.items() if o.status is RouteStatus.SUCCESS]
            emit(state="optimizing", phase="OPTIMIZING", total_nets=len(done_nets))
            report = optimize_nets(
                fork, done_nets, OptimizeGoal.FEWER_VIAS, control=control, deadline=deadline
            )
            job_log += report.log
        emit_partial(force=True)  # optimisation replaced copper: re-snapshot
        # final bookkeeping from the fork's diff to the base
        final = fork.board
        final_ids = {t.id for t in final.tracks} | {v.id for v in final.vias}
        added_tracks = tuple(t for t in final.tracks if t.id not in base_ids)
        added_vias = tuple(v for v in final.vias if v.id not in base_ids)
        removed = tuple(sorted(base_ids - final_ids))
        for o in outcomes.values():
            o.added_ids = [t.id for t in added_tracks if t.net_name == o.net] + [
                v.id for v in added_vias if v.net_name == o.net
            ]
            o.length_nm = sum(t.length for t in added_tracks if t.net_name == o.net)
            o.vias = sum(1 for v in added_vias if v.net_name == o.net)
            o.removed_ids = [i for i in o.removed_ids if i in removed]
        emit(state="validating", phase="VALIDATING")
        geo = fork.engine.geometry
        completed = 0
        for net, o in outcomes.items():
            full = net_connectivity(geo, net).status in (
                NetStatus.FULLY_CONNECTED,
                NetStatus.NOT_APPLICABLE,
            )
            if full:
                completed += 1
                o.status = RouteStatus.SUCCESS
            elif o.status is RouteStatus.SUCCESS:
                o.status = RouteStatus.PARTIAL
        if cancelled or out_of_time:
            why = (
                "cancelled by the user"
                if cancelled
                else f"the board time budget ({s.budget_s:.0f} s) ran out"
            )
            for o in outcomes.values():
                if o.status is RouteStatus.NO_ROUTE and o.reason is None and not o.message:
                    o.status = RouteStatus.CANCELLED if cancelled else RouteStatus.TIMEOUT
                    o.reason = FailureReason.TIMEOUT
                    o.message = f"not attempted: {why}"
            job_log.append(f"stopped: {why}")
        metrics.nets_completed = completed
        metrics.nets_failed = len(outcomes) - completed
        metrics.total_length_nm = sum(t.length for t in added_tracks)
        metrics.new_vias = len(added_vias)
        metrics.runtime_s = time.perf_counter() - t0
        if not plan.tasks:
            status = BoardStatus.NOTHING_TO_ROUTE
        elif cancelled:
            status = BoardStatus.CANCELLED
        elif completed == len(plan.tasks):
            status = BoardStatus.FULLY_ROUTED
        elif completed or added_tracks:
            status = BoardStatus.PARTIALLY_ROUTED
        else:
            status = BoardStatus.FAILED
        removed_nets = _removed_nets(base_board, removed)
        result = BoardRoutingResult(
            status,
            base_board,
            final,
            plan,
            outcomes,
            metrics,
            added_tracks,
            added_vias,
            removed,
            job_log,
            removed_nets,
            copper_dependencies(base_board, added_tracks, added_vias, removed_nets),
        )
        log.info("board_router.done %s metrics=%s", result.summary(), metrics.to_dict())
        emit(state="done")
        return result

    # ------------------------------------------------------------ one net
    def _request(self, task: RouteTask, pass_no: int) -> RouteRequest:
        base = task.request or self.settings.base_request
        req = replace(base, net=task.net, candidates=1, request_id=f"board-{task.net}-p{pass_no}")
        if pass_no == 1 and self.settings.max_passes > 1:
            n_tasks = max(1, len(self._all_tasks))
            slice_s = max(PASS1_MIN_SLICE_S, PASS1_SLICE * self.settings.budget_s / n_tasks)
            total = req.total_time_limit_s
            req = replace(
                req,
                node_limit=min(req.node_limit, PASS1_NODE_LIMIT),
                total_time_limit_s=slice_s if total is None else min(total, slice_s),
            )
        if pass_no >= 2:  # R5: spend harder only where limits (not space) bound us
            last = self._outcomes.get(task.net)
            last_reason = last.reason if last is not None else None
            if last_reason is None or last_reason in (
                FailureReason.TIMEOUT,
                FailureReason.VIA_LIMIT,
                FailureReason.LAYER_RESTRICTION,
            ):
                # hit a limit last time: more search may break through
                req = replace(
                    req, node_limit=req.node_limit * 2, time_limit_s=req.time_limit_s * 1.5
                )
            # NO_PATH on a crowded board: look closer once (finer cells resolve
            # tight channels between existing copper). Single step: deeper
            # refinement belongs to explicit user choice, not silent retries.
            if (
                pass_no == 2
                and last_reason is FailureReason.NO_PATH
                and req.grid_resolution > FINE_GRID_FLOOR_NM
            ):
                req = replace(
                    req, grid_resolution=max(FINE_GRID_FLOOR_NM, req.grid_resolution // 2)
                )
            # NO_ESCAPE / VALIDATION / RULE_UNKNOWN: the space is exhausted or
            # forbidden — retry at base budget (often rip-up helps)
            share = max(PASS2_MIN_SLICE_S, PASS2_SHARE * self.settings.budget_s)
            total = req.total_time_limit_s
            req = replace(req, total_time_limit_s=share if total is None else min(total, share))
        if self._deadline is not None:
            # the job budget also bounds the work *inside* one net
            left = max(0.001, self._deadline - time.perf_counter())
            total = req.total_time_limit_s
            req = replace(req, total_time_limit_s=left if total is None else min(total, left))
        return req

    def _route_task(
        self,
        fork: WorkingBoard,
        task: RouteTask,
        pass_no: int,
        outcomes: dict[str, NetOutcome],
        metrics: BoardMetrics,
        control: BoardRoutingControl,
        job_log: list[str],
    ) -> bool:
        router = self.router_factory(fork.engine)
        penalties = _congestion_provider(fork) if pass_no >= 2 else None
        req = self._task_request(fork, task, pass_no)
        res = route_net_refined(router, req, control.cancel_event, penalties)
        applied = self._apply_result(fork, task, pass_no, res, outcomes, metrics, job_log)
        if applied is None:  # should not happen: the fork is the router's own state
            o = outcomes[task.net]
            o.status, o.reason = RouteStatus.NO_ROUTE, FailureReason.VALIDATION
            return False
        return applied

    def _task_request(self, fork: WorkingBoard, task: RouteTask, pass_no: int) -> RouteRequest:
        req = self._request(task, pass_no)
        if task.kind is TaskKind.DIFF_PAIR and task.group:
            from pcbrouter.routing.diffpair import pair_request

            req = pair_request(fork, req, task.group)
        return req

    def _apply_result(
        self,
        fork: WorkingBoard,
        task: RouteTask,
        pass_no: int,
        res: Any,
        outcomes: dict[str, NetOutcome],
        metrics: BoardMetrics,
        job_log: list[str],
    ) -> bool | None:
        """Record ``res`` for ``task`` and commit its copper to the fork (validated).
        Returns None when the commit is refused (copper no longer legal here)."""
        metrics.absorb(res.metrics)
        o = outcomes[task.net]
        o.passes = pass_no
        o.attempts += 1
        o.expanded_nodes += int(res.metrics.expanded_nodes)
        o.route_s += float(res.metrics.elapsed_s)
        if res.status is RouteStatus.ALREADY_CONNECTED:
            o.status = RouteStatus.SUCCESS
            metrics.clean_nets += 1  # nothing to route: trivially clean
            return True
        if res.best is None:
            o.status, o.reason, o.message = res.status, res.reason, _failure_text(res)
            job_log.append(
                f"pass {pass_no}: {task.net} {res.status.value} "
                f"{res.reason.value if res.reason else ''} {res.message}".strip()
            )
            return False
        try:
            fork.commit_proposals(
                [res.best.proposal], f"board route {task.net}", Provenance.ROUTER_GENERATED
            )
        except CommitError as exc:
            o.message = str(exc)
            return None
        o.status = res.status
        o.reason = res.reason if res.status is RouteStatus.PARTIAL else None
        o.message = _failure_text(res) if res.status is RouteStatus.PARTIAL else res.message
        if res.status is RouteStatus.SUCCESS and res.metrics.repairs == 0:
            metrics.clean_nets += 1
        return res.status is RouteStatus.SUCCESS

    # ------------------------------------------------------------ parallel
    def _start_parallel(
        self, fork: WorkingBoard, plan: BoardRoutingPlan, job_log: list[str]
    ) -> Any:
        from pcbrouter.routing.parallel import (
            PARALLEL_MIN_SPEEDUP,
            PARALLEL_MIN_TASKS,
            ParallelRouter,
            estimate_speedup,
        )

        workers = self.settings.parallel_workers
        if workers <= 1 or len(plan.tasks) < PARALLEL_MIN_TASKS:
            return None
        geo = fork.engine.geometry
        self._regions: dict[str, Any] = {}
        for t in plan.tasks:
            boxes = [geo.copper[u].bounds for u in net_connectivity(geo, t.net).pad_uids
                     if u in geo.copper]  # fmt: skip
            box = boxes[0] if boxes else None
            for b in boxes[1:]:
                box = box.union(b) if box is not None else b
            self._regions[t.net] = box
        gain = estimate_speedup(plan.tasks, self._regions, workers)
        if gain < PARALLEL_MIN_SPEEDUP:
            job_log.append(
                f"parallel routing skipped: the nets overlap too much to route side by side "
                f"(estimated {gain:.1f}x with {workers} helpers); routing on one worker"
            )
            return None
        try:
            par = ParallelRouter(fork, workers)
        except Exception as exc:  # routing continues sequentially, and says so
            log.warning("parallel.unavailable %s", exc)
            job_log.append(f"parallel routing unavailable ({exc}); routing sequentially")
            return None
        job_log.append(f"parallel routing: {par.workers} helper processes (estimated {gain:.1f}x)")
        return par

    def _parallel_pass(
        self,
        fork: WorkingBoard,
        failed: list[RouteTask],
        still: list[RouteTask],
        pass_no: int,
        outcomes: dict[str, NetOutcome],
        metrics: BoardMetrics,
        control: BoardRoutingControl,
        job_log: list[str],
        par: Any,
        emit: Callable[..., None],
        emit_partial: Callable[..., None],
        total_nets: int,
    ) -> str:
        """One pass as a work queue over the helper processes (routing/parallel.py):
        a free helper gets the next net whose region does not overlap a net in
        flight; results are committed through the validator as they arrive, a
        conflict is re-queued once and then routed here. Returns "", "cancelled"
        or "out_of_time"."""
        from pcbrouter.routing.parallel import LOOKAHEAD, REGION_MARGIN_NM

        deadline = self._deadline or math.inf
        pending = list(failed)
        retried: set[str] = set()
        inflight: dict[int, tuple[RouteTask, BoundingBox | None]] = {}
        done = 0
        stop = ""

        def region(task: RouteTask) -> BoundingBox | None:
            box = self._regions.get(task.net)
            return None if box is None else box.expanded(REGION_MARGIN_NM)

        def dispatchable() -> RouteTask | None:
            busy = [b for _t, b in inflight.values()]
            for task in pending[: max(1, par.workers * LOOKAHEAD)]:
                box = region(task)
                if box is None:
                    if not inflight:
                        return task
                    continue
                if not any(b is None or b.intersects(box) for b in busy):
                    return task
            return None

        while pending or inflight:
            if not stop and (not control.checkpoint(deadline) or time.perf_counter() > deadline):
                stop = "cancelled" if control.cancel_event.is_set() else "out_of_time"
                par.cancel.set()  # in-flight searches stop at their next check
            if not stop:
                for h in range(par.workers):
                    if h in inflight or not pending:
                        continue
                    task = dispatchable()
                    if task is None:
                        break
                    pending.remove(task)
                    par.dispatch(h, self._task_request(fork, task, pass_no), pass_no >= 2)
                    inflight[h] = (task, region(task))
                    emit(
                        net=task.net, index=done + len(inflight), total=len(failed),
                        pass_no=pass_no, state="routing", phase="ROUTING",
                        total_nets=total_nets,
                        message=f"{len(inflight)} of {par.workers} parallel helpers busy",
                        completed_nets=sum(
                            1 for o in outcomes.values() if o.status is RouteStatus.SUCCESS
                        ),
                    )  # fmt: skip
            elif not inflight:
                break
            got = par.poll(set(inflight))
            if got is None:
                continue
            h, kind, payload = got
            if h not in inflight:
                continue
            task, _box = inflight.pop(h)
            if stop:
                still.append(task)  # discard: the job is stopping
                continue
            ok: bool | None = None
            if kind == "result":
                ok = self._apply_result(fork, task, pass_no, payload, outcomes, metrics, job_log)
            else:
                log.warning("parallel.helper_error net=%s %s", task.net, payload)
                job_log.append(f"parallel helper error on {task.net}; routed sequentially")
            if ok is None:
                metrics.parallel_conflicts += 1
                if kind == "result" and task.net not in retried:
                    retried.add(task.net)
                    pending.insert(0, task)  # copper committed meanwhile: try again
                    continue
                ok = self._route_task(fork, task, pass_no, outcomes, metrics, control, job_log)
            if not ok:
                still.append(task)
            done += 1
            metrics.parallel_batches += 1
            emit_partial()
        if stop:
            still.extend(pending)
            par.cancel.clear()
        return stop

    # ------------------------------------------------------------ rip-up
    def _rippable(self, fork: WorkingBoard) -> list[str]:
        # copper the user accepted (or applied from an optimisation) is
        # user-approved: only ripped with the explicit setting
        ok = {Provenance.ROUTER_GENERATED}
        if self.settings.ripup_user_accepted:
            ok |= {Provenance.USER_ACCEPTED, Provenance.OPTIMIZER}
        idx = fork.board.index
        keep = self._base_ids if self.settings.preserve_existing else set()
        out = []
        for obj_id, prov in fork.provenance.items():
            obj = idx.tracks_by_id.get(obj_id) or idx.vias_by_id.get(obj_id)
            if obj is None or prov not in ok or fork.is_locked(obj_id, obj.net_name):
                continue
            if obj_id in keep:
                continue
            out.append(obj_id)
        return sorted(out)

    def _ripup_pass(
        self,
        fork: WorkingBoard,
        failed: list[RouteTask],
        outcomes: dict[str, NetOutcome],
        metrics: BoardMetrics,
        control: BoardRoutingControl,
        job_log: list[str],
        deadline: float,
    ) -> list[RouteTask]:
        s = self.settings
        remaining: list[RouteTask] = []
        tries = self._ripup_tries
        for task in failed:
            if not control.checkpoint(deadline) or time.perf_counter() > deadline:
                remaining.append(task)
                continue
            if (
                metrics.ripups >= s.max_total_ripups
                or tries.get(task.net, 0) >= s.max_ripups_per_net
            ):
                remaining.append(task)
                continue
            tries[task.net] = tries.get(task.net, 0) + 1
            self._emit(
                state="ripup", phase="RIPUP_REROUTE", net=task.net, ripup_try=tries[task.net]
            )
            if not self._try_ripup(fork, task, outcomes, metrics, control, job_log):
                remaining.append(task)
        return remaining

    def _try_ripup(
        self,
        fork: WorkingBoard,
        task: RouteTask,
        outcomes: dict[str, NetOutcome],
        metrics: BoardMetrics,
        control: BoardRoutingControl,
        job_log: list[str],
    ) -> bool:
        rippable = self._rippable(fork)
        rippable = [i for i in rippable if self._net_of(fork, i) != task.net]
        if not rippable:
            return False
        snapshot_len = len(fork.commits)
        before_done = self._connected_count(fork, outcomes)
        geo0 = fork.engine.geometry
        displaced_before = {
            n: net_connectivity(geo0, n).status is NetStatus.FULLY_CONNECTED
            for n in {self._net_of(fork, i) for i in rippable}
            if n is not None
        }
        # 1) route the failed net on a board without the rippable copper
        idx = fork.board.index
        displaced_nets = sorted(
            {n for n in (self._net_of(fork, i) for i in rippable) if n is not None}
        )
        try:
            fork.commit_objects((), (), rippable, f"rip-up for {task.net}", validate=False)
        except CommitError:
            return False
        res = route_net_refined(
            self.router_factory(fork.engine), self._request(task, 3), control.cancel_event
        )
        metrics.absorb(res.metrics)
        if res.best is None or res.status is not RouteStatus.SUCCESS:
            self._rollback(fork, snapshot_len)
            return False
        fork.commit_proposals([res.best.proposal], f"board route {task.net} (after rip-up)")
        # 2) put back every removed route that still fits; reroute the others
        kept_back: list[str] = []
        by_net: dict[str | None, list[str]] = {}
        for i in rippable:
            by_net.setdefault(self._net_of_obj(idx, i), []).append(i)
        restored_ids: set[str] = set()
        for net in displaced_nets:
            ids = by_net.get(net, [])
            tracks = [idx.tracks_by_id[i] for i in ids if i in idx.tracks_by_id]
            vias = [idx.vias_by_id[i] for i in ids if i in idx.vias_by_id]
            try:
                fork.commit_objects(tracks, vias, (), f"restore {net}", Provenance.ROUTER_GENERATED)
                restored_ids.update(ids)
                kept_back.append(str(net))
                continue
            except CommitError:
                pass
            metrics.reroutes += 1
            other = next((t for t in self._all_tasks if t.net == net), RouteTask(str(net)))
            sub = route_net_refined(
                self.router_factory(fork.engine), self._request(other, 3), control.cancel_event
            )
            metrics.absorb(sub.metrics)
            if sub.best is not None:
                fork.commit_proposals([sub.best.proposal], f"reroute {net}")
        after_done = self._connected_count(fork, outcomes)
        geo1 = fork.engine.geometry
        broken = sorted(
            n for n, was in displaced_before.items()
            if was and net_connectivity(geo1, n).status is not NetStatus.FULLY_CONNECTED
        )  # fmt: skip
        if broken:
            # a rip-up may move another route, never leave it disconnected —
            # including nets outside this job (routed and accepted earlier)
            self._rollback(fork, snapshot_len)
            job_log.append(
                f"rip-up for {task.net} would disconnect {', '.join(broken)}; rolled back"
            )
            return False
        if after_done <= before_done:
            self._rollback(fork, snapshot_len)
            job_log.append(f"rip-up for {task.net} did not improve completion; rolled back")
            return False
        metrics.ripups += 1
        removed_for_net = [i for i in rippable if i not in restored_ids]
        outcomes[task.net].status = RouteStatus.SUCCESS
        outcomes[task.net].reason = None
        outcomes[task.net].removed_ids = removed_for_net
        job_log.append(
            f"rip-up for {task.net}: {len(removed_for_net)} generated object(s) removed, "
            f"{len(kept_back)} route(s) restored, completion {before_done}→{after_done}"
        )
        return True

    @staticmethod
    def _rollback(fork: WorkingBoard, length: int) -> None:
        while len(fork.commits) > length:
            fork.undo()

    @staticmethod
    def _net_of(fork: WorkingBoard, obj_id: str) -> str | None:
        return BoardRouter._net_of_obj(fork.board.index, obj_id)

    @staticmethod
    def _net_of_obj(idx: Any, obj_id: str) -> str | None:
        obj = idx.tracks_by_id.get(obj_id) or idx.vias_by_id.get(obj_id)
        return obj.net_name if obj is not None else None

    @staticmethod
    def _connected_count(fork: WorkingBoard, outcomes: dict[str, NetOutcome]) -> int:
        geo = fork.engine.geometry
        return sum(
            1
            for net in outcomes
            if net_connectivity(geo, net).status
            in (NetStatus.FULLY_CONNECTED, NetStatus.NOT_APPLICABLE)
        )


def _congestion_provider(
    fork: WorkingBoard,
) -> Callable[[str, GridSpec], npt.NDArray[np.float64] | None]:
    """Congestion feedback: per-cell density of copper already on the layer
    (geometric estimate, 0..1), sampled on the search grid."""
    engine = fork.engine

    def provider(layer: str, spec: GridSpec) -> npt.NDArray[np.float64] | None:
        try:
            cmap = engine.congestion(layer)
        except Exception:  # congestion is advisory; never block routing on it
            log.debug("congestion unavailable for %s", layer, exc_info=True)
            return None
        ys = spec.origin_y + (np.arange(spec.ny) + 0.5) * spec.cell
        xs = spec.origin_x + (np.arange(spec.nx) + 0.5) * spec.cell
        rows = np.clip(
            ((ys - cmap.origin_y) // cmap.tile).astype(np.int64), 0, cmap.values.shape[0] - 1
        )
        cols = np.clip(
            ((xs - cmap.origin_x) // cmap.tile).astype(np.int64), 0, cmap.values.shape[1] - 1
        )
        return np.asarray(cmap.values[np.ix_(rows, cols)], dtype=np.float64)

    return provider


__all__ = [
    "BoardRouter",
    "BoardRouterSettings",
    "BoardRoutingControl",
    "BoardRoutingPlan",
    "BoardRoutingResult",
    "BoardStatus",
    "NetOutcome",
    "RouteGroup",
    "RouteTask",
    "Strategy",
    "TaskKind",
    "diff_pairs",
    "make_plan",
    "order_tasks",
]
