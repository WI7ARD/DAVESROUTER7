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
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

import numpy as np
import numpy.typing as npt

from pcbrouter.domain.board import Board
from pcbrouter.domain.track import Track
from pcbrouter.domain.via import Via
from pcbrouter.routing.connectivity import NetStatus, net_connectivity
from pcbrouter.routing.occupancy import GridSpec
from pcbrouter.routing.request import RouteRequest, with_user_constraints
from pcbrouter.routing.result import FailureReason, RouteStatus
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, Provenance, WorkingBoard

log = logging.getLogger(__name__)

DEFAULT_MAX_PASSES = 3
DEFAULT_MAX_RIPUPS_PER_NET = 2
DEFAULT_MAX_TOTAL_RIPUPS = 20
DEFAULT_BUDGET_S = 600.0
CONGESTION_TILE_CELLS = 10  # congestion field resolution for feedback (cells per tile)


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


@dataclass
class BoardRouterSettings:
    strategy: Strategy = Strategy.CRITICAL_FIRST
    max_passes: int = DEFAULT_MAX_PASSES
    allow_ripup: bool = True
    ripup_user_accepted: bool = False
    max_ripups_per_net: int = DEFAULT_MAX_RIPUPS_PER_NET
    max_total_ripups: int = DEFAULT_MAX_TOTAL_RIPUPS
    budget_s: float = DEFAULT_BUDGET_S
    base_request: RouteRequest = field(default_factory=lambda: RouteRequest("", candidates=1))
    optimize: bool = False
    priorities: dict[str, int] = field(default_factory=dict)
    groups: tuple[RouteGroup, ...] = ()


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

    def checkpoint(self) -> bool:
        """Block while paused; return False when cancelled."""
        while not self._running.wait(0.1):
            pass
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

    @property
    def completion(self) -> float:
        return self.nets_completed / self.nets_attempted if self.nets_attempted else 1.0

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
        }


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

    def summary(self) -> str:
        m = self.metrics
        return (
            f"ROUTER RESULT board: {self.status.value} — {m.nets_completed}/{m.nets_attempted} "
            f"nets ({100 * m.completion:.0f} %), {m.new_vias} new via(s), "
            f"{m.total_length_nm / 1e6:.1f} mm, {m.ripups} rip-up(s), {m.runtime_s:.1f} s"
        )

    def objects_for(self, nets: set[str] | None = None) -> tuple[list[Track], list[Via], list[str]]:
        """Copper to add and generated ids to remove for accepting ``nets`` (all when
        None). Rip-ups required by an accepted net are always included."""
        tracks = [t for t in self.added_tracks if nets is None or t.net_name in nets]
        vias = [v for v in self.added_vias if nets is None or v.net_name in nets]
        if nets is None:
            removed = list(self.removed_ids)
        else:
            removed = sorted(
                {i for n in nets for i in self.outcomes[n].removed_ids} if nets else set()
            )
            removed = [i for i in removed if i in set(self.removed_ids)]
        return tracks, vias, removed


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


def _task_features(wb: WorkingBoard, net: str) -> tuple[int, float, int]:
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
    return len(conn.pad_uids), length, escapes


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
    partner = {a: b for a, b in pairs} | {b: a for a, b in pairs}
    group_of = {n: g.name for g in settings.groups for n in g.nets}
    group_prio = {g.name: g.priority for g in settings.groups}
    resolver = engine.resolver
    default_w = resolver.resolve_trace_width(None).value or 0
    tasks: list[RouteTask] = []
    for net in candidates:
        pads, length, escapes = _task_features(wb, net)
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
                net, kind, prio, partner.get(net) or group_of.get(net), req, pads, length, escapes
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
    def __init__(
        self,
        working: WorkingBoard,
        settings: BoardRouterSettings | None = None,
        router_factory: Callable[[Any], Router] | None = None,
    ) -> None:
        self.base = working
        self.settings = settings or BoardRouterSettings()
        self.router_factory = router_factory or (lambda engine: Router(engine))
        self._all_tasks: list[RouteTask] = []
        self._deadline: float | None = None
        self._emit: Callable[..., None] = lambda **_kw: None

    def run(
        self,
        plan: BoardRoutingPlan | None = None,
        control: BoardRoutingControl | None = None,
        progress: Progress | None = None,
    ) -> BoardRoutingResult:
        t0 = time.perf_counter()
        control = control or BoardRoutingControl()
        s = self.settings
        fork = self.base.fork()
        base_board = fork.board
        plan = plan or make_plan(fork, s)
        self._all_tasks = list(plan.tasks)
        metrics = BoardMetrics(nets_attempted=len(plan.tasks))
        outcomes: dict[str, NetOutcome] = {
            t.net: NetOutcome(t.net, RouteStatus.NO_ROUTE) for t in plan.tasks
        }
        job_log: list[str] = []
        deadline = t0 + s.budget_s
        self._deadline = deadline
        cancelled = False

        def emit(**kw: Any) -> None:
            if progress is not None:
                progress({"metrics": metrics.to_dict(), "total_passes": s.max_passes, **kw})

        self._emit = emit
        emit(state="planning", phase="PLANNING", total_nets=len(plan.tasks))

        failed = [t for t in plan.tasks]
        for pass_no in range(1, max(1, s.max_passes) + 1):
            if not failed:
                break
            metrics.passes = pass_no
            still: list[RouteTask] = []
            for i, task in enumerate(failed):
                if not control.checkpoint() or time.perf_counter() > deadline:
                    cancelled = True
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
            if cancelled:
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
        if s.optimize and not cancelled:
            from pcbrouter.routing.optimize import OptimizeGoal, optimize_nets

            done_nets = [n for n, o in outcomes.items() if o.status is RouteStatus.SUCCESS]
            emit(state="optimizing", phase="OPTIMIZING", total_nets=len(done_nets))
            report = optimize_nets(
                fork, done_nets, OptimizeGoal.FEWER_VIAS, control=control, deadline=deadline
            )
            job_log += report.log
        # final bookkeeping from the fork's diff to the base
        base_ids = {t.id for t in base_board.tracks} | {v.id for v in base_board.vias}
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
        )
        log.info("board_router.done %s metrics=%s", result.summary(), metrics.to_dict())
        emit(state="done")
        return result

    # ------------------------------------------------------------ one net
    def _request(self, task: RouteTask, pass_no: int) -> RouteRequest:
        base = task.request or self.settings.base_request
        req = replace(base, net=task.net, candidates=1, request_id=f"board-{task.net}-p{pass_no}")
        if pass_no >= 2:  # retries search harder
            req = replace(req, node_limit=req.node_limit * 2, time_limit_s=req.time_limit_s * 1.5)
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
        engine = fork.engine
        router = self.router_factory(engine)
        penalties = _congestion_provider(fork) if pass_no >= 2 else None
        req = self._request(task, pass_no)
        if task.kind is TaskKind.DIFF_PAIR and task.group:
            from pcbrouter.routing.diffpair import pair_request

            req = pair_request(fork, req, task.group)
        res = router.route_net(req, cancel=control.cancel_event, penalties=penalties)
        metrics.expanded_nodes += res.metrics.expanded_nodes
        o = outcomes[task.net]
        o.passes = pass_no
        if res.status is RouteStatus.ALREADY_CONNECTED:
            o.status = RouteStatus.SUCCESS
            return True
        if res.best is None:
            o.status, o.reason, o.message = res.status, res.reason, res.message
            job_log.append(
                f"pass {pass_no}: {task.net} {res.status.value} "
                f"{res.reason.value if res.reason else ''} {res.message}".strip()
            )
            return False
        try:
            fork.commit_proposals(
                [res.best.proposal], f"board route {task.net}", Provenance.ROUTER_GENERATED
            )
        except CommitError as exc:  # should not happen: the fork is the router's own state
            o.status, o.reason, o.message = RouteStatus.NO_ROUTE, FailureReason.VALIDATION, str(exc)
            return False
        o.status = res.status
        o.reason = res.reason if res.status is RouteStatus.PARTIAL else None
        o.message = res.message
        return res.status is RouteStatus.SUCCESS

    # ------------------------------------------------------------ rip-up
    def _rippable(self, fork: WorkingBoard) -> list[str]:
        ok = {Provenance.ROUTER_GENERATED, Provenance.OPTIMIZER}
        if self.settings.ripup_user_accepted:
            ok.add(Provenance.USER_ACCEPTED)
        idx = fork.board.index
        out = []
        for obj_id, prov in fork.provenance.items():
            obj = idx.tracks_by_id.get(obj_id) or idx.vias_by_id.get(obj_id)
            if obj is None or prov not in ok or fork.is_locked(obj_id, obj.net_name):
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
        tries: dict[str, int] = {}
        for task in failed:
            if not control.checkpoint() or time.perf_counter() > deadline:
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
        # 1) route the failed net on a board without the rippable copper
        idx = fork.board.index
        displaced_nets = sorted(
            {n for n in (self._net_of(fork, i) for i in rippable) if n is not None}
        )
        try:
            fork.commit_objects((), (), rippable, f"rip-up for {task.net}", validate=False)
        except CommitError:
            return False
        res = self.router_factory(fork.engine).route_net(
            self._request(task, 3), cancel=control.cancel_event
        )
        metrics.expanded_nodes += res.metrics.expanded_nodes
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
            sub = self.router_factory(fork.engine).route_net(
                self._request(other, 3), cancel=control.cancel_event
            )
            metrics.expanded_nodes += sub.metrics.expanded_nodes
            if sub.best is not None:
                fork.commit_proposals([sub.best.proposal], f"reroute {net}")
        after_done = self._connected_count(fork, outcomes)
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
