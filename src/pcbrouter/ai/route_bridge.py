"""AI planner → deterministic router (Stage 7).

The AI decides *what* to attempt; this module turns an **approved, validated**
:class:`AICommand` into router requests; the router decides *how*; the Stage 3
validator decides legality; the user accepts. Authority flows downward only::

    AI command ─validate─▶ user approval ─▶ plan_from_command ─▶ Router / BoardRouter
         ─▶ candidates (validated) ─▶ preview ─▶ user Accept ─▶ working board

Nothing the model writes is applied as geometry: AI areas become *soft* corridors,
widths/vias/layers become request constraints that the rule engine may only
tighten (a width below a rule minimum makes the request INVALID). Deterministic
router results are fed back to the planner as ``ROUTER RESULT`` facts.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from pcbrouter.ai.command_schema import (
    AICommand,
    AreaTarget,
    BoardTarget,
    NetGroupTarget,
    NetTarget,
    Operation,
    OperationCategory,
)
from pcbrouter.domain.geometry import BoundingBox
from pcbrouter.domain.units import Nm, mm_to_internal
from pcbrouter.routing.request import RouteRequest, SoftRegion, SoftRegionKind

if TYPE_CHECKING:
    from pcbrouter.compute.manager import ComputeManager
    from pcbrouter.domain.board import Board
    from pcbrouter.routing.board_router import BoardRouterSettings, BoardRoutingResult
    from pcbrouter.routing.optimize import OptimizeReport
    from pcbrouter.routing.result import RouteResult
    from pcbrouter.routing.working_board import WorkingBoard

#: planner follow-ups per plan (bounded engineering loop; each is a user action)
MAX_PLANNING_ROUNDS = 3
MAX_ROUTER_FACTS = 10


class AutonomyMode(Enum):
    ADVISORY = "advisory"  # AI analyses only: routing proposals cannot run
    APPROVAL_REQUIRED = "approval_required"  # default: every modifying command approved
    BATCH_APPROVAL = "batch_approval"  # user approves a bounded plan at once


class BridgeError(ValueError):
    """The command cannot be executed (wrong state, unsupported, unsafe)."""


#: AI priority / criticality words → board-routing order (higher routes earlier)
PRIORITY_RANK = {"low": -1, "normal": 0, "high": 2, "critical": 3}
CRITICALITY_RANK = {"low": -1, "normal": 0, "high": 2, "safety_critical": 3}


@dataclass(frozen=True)
class RequestConstraints:
    """The request-level part of approved AI constraints. The rule engine still
    validates every value (:func:`pcbrouter.routing.request.normalise`)."""

    preferred_width: Nm | None = None
    preferred_layers: tuple[str, ...] = ()
    forbidden_layers: tuple[str, ...] = ()
    max_vias: int | None = None
    minimize_vias: bool = False
    soft_regions: tuple[SoftRegion, ...] = ()
    note: str = ""

    def apply(self, req: RouteRequest) -> RouteRequest:
        return replace(
            req,
            preferred_width=(
                self.preferred_width if self.preferred_width is not None else req.preferred_width
            ),
            preferred_layers=self.preferred_layers or req.preferred_layers,
            forbidden_layers=tuple(dict.fromkeys(req.forbidden_layers + self.forbidden_layers)),
            max_vias=self.max_vias if self.max_vias is not None else req.max_vias,
            minimize_vias=self.minimize_vias or req.minimize_vias,
            soft_regions=req.soft_regions + self.soft_regions,
            constraints_note=self.note or req.constraints_note,
        )


@dataclass(frozen=True)
class BoardPolicy:
    """The one routing policy of an AI plan. Execution builds its
    :class:`BoardRouterSettings` from it (:meth:`settings`) and the approval panel
    describes those same settings (:func:`describe_settings`), so what the user
    approves is what runs."""

    allow_ripup: bool = False
    preserve_existing: bool = True
    #: the AI schema cannot grant this: copper the user accepted is never ripped
    ripup_user_accepted: bool = False
    mode: str = "accuracy"  # RouteMode value
    budget_s: float | None = None  # None = the board router default
    parallel_workers: int = 0
    priorities: tuple[tuple[str, int], ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()
    request: RequestConstraints = RequestConstraints()

    def settings(self) -> BoardRouterSettings:
        from pcbrouter.routing.board_router import BoardRouterSettings
        from pcbrouter.routing.presets import RouteMode, adjust_board_settings

        base = self.request.apply(RouteRequest("", candidates=1))
        st = BoardRouterSettings(
            allow_ripup=self.allow_ripup,
            ripup_user_accepted=self.ripup_user_accepted,
            preserve_existing=self.preserve_existing,
            base_request=base,
            priorities=dict(self.priorities),
            pairs=self.pairs,
            parallel_workers=self.parallel_workers,
        )
        if self.budget_s is not None:
            st = replace(st, budget_s=self.budget_s)
        return adjust_board_settings(st, base, RouteMode(self.mode))


def describe_settings(
    settings: BoardRouterSettings, policy: BoardPolicy | None = None
) -> list[str]:
    """Plain-language lines for the settings a board job really runs with."""
    speed = settings.base_request.heuristic_weight > 1.0  # the Speed preset's marker
    mode = policy.mode.capitalize() if policy is not None else ("Speed" if speed else "Accuracy")
    lines = [f"Mode: {mode}"]
    if not settings.allow_ripup:
        rip = "Rip-up: off"
        if policy is not None and policy.allow_ripup:
            rip += " (Speed mode never rips up)"
        lines.append(rip)
        lines.append("Existing routes: kept exactly; only new copper is added")
    else:
        lines.append(
            f"Rip-up: on, at most {settings.max_ripups_per_net} per net and "
            f"{settings.max_total_ripups} per job"
        )
        if settings.preserve_existing:
            lines.append(
                "Existing routes: kept exactly; rip-up may only move copper created by this job"
            )
        else:
            lines.append(
                "Existing routes: router-made routes may be moved and rerouted "
                "(a move that would disconnect a net is rolled back)"
            )
        lines.append(
            "Routes you accepted: "
            + ("may be moved" if settings.ripup_user_accepted else "never moved")
            + "; routes from the file and locked copper: never moved"
        )
    lines.append(f"Time budget: {settings.budget_s:.0f} s")
    req = settings.base_request
    if req.preferred_layers:
        lines.append("Preferred layers (soft): " + ", ".join(req.preferred_layers))
    if req.forbidden_layers:
        lines.append("Forbidden layers: " + ", ".join(req.forbidden_layers))
    if req.max_vias is not None:
        lines.append(f"At most {req.max_vias} via(s) per net")
    if req.minimize_vias:
        lines.append("Vias cost more (soft)")
    first = sorted((kv for kv in settings.priorities.items() if kv[1] > 0), key=lambda kv: -kv[1])
    if first:
        lines.append("Routed first (soft): " + ", ".join(n for n, _v in first))
    for a, b in settings.pairs:
        lines.append(
            f"Differential pair {a}/{b}: routed together with a soft corridor; "
            "gap, skew and impedance are not controlled"
        )
    return lines


@dataclass(frozen=True)
class AnalysisTarget:
    """ANALYSIS_ONLY constraints: measured after routing, never enforced."""

    net: str
    max_length_mm: float | None = None
    target_length_mm: float | None = None
    tolerance_mm: float | None = None
    pair: str | None = None  # partner net of a differential pair
    pair_gap_mm: float | None = None
    pair_skew_mm: float | None = None


@dataclass
class ExecutionPlan:
    kind: str  # "route_nets" | "route_board" | "optimize"
    requests: list[RouteRequest] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    goal: Any = None  # OptimizeGoal
    notes: list[str] = field(default_factory=list)
    #: board jobs: the single source of the router settings
    policy: BoardPolicy | None = None
    #: board jobs: per-net request constraints (route_group / batch)
    constraints: dict[str, RequestConstraints] = field(default_factory=dict)
    analysis: list[AnalysisTarget] = field(default_factory=list)

    def describe(self) -> list[str]:
        """What will run — derived from the same objects execution uses."""
        if self.kind == "route_board" and self.policy is not None:
            out = describe_settings(self.policy.settings(), self.policy)
            out.insert(0, f"Nets: {', '.join(self.nets)}" if self.nets else "Nets: every open net")
            return out
        if self.kind == "route_nets":
            return [
                f"Nets: {', '.join(self.nets)} (one at a time)",
                "Single-net routing only adds copper: no existing route is moved",
            ]
        return [
            f"Optimise {', '.join(self.nets)} for {getattr(self.goal, 'value', self.goal)}",
            "Only these nets' own routes are replaced; the result is shown for review",
        ]


@dataclass
class ExecutionOutcome:
    plan: ExecutionPlan
    route_results: list[RouteResult] = field(default_factory=list)
    board_result: BoardRoutingResult | None = None
    optimize_reports: list[OptimizeReport] = field(default_factory=list)
    #: the fork's board after an optimisation plan (applied by the user as one commit)
    optimized_board: Any = None
    #: the settings lines the board job actually ran with
    policy_lines: list[str] = field(default_factory=list)
    #: ANALYSIS_ONLY results (length / pair metrics vs. the approved targets)
    analysis_lines: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        if self.board_result is not None:
            return bool(self.board_result.added_tracks or self.board_result.added_vias)
        if self.route_results:
            return any(r.success for r in self.route_results)
        return any(r.improved for r in self.optimize_reports)

    def summary(self) -> str:
        if self.board_result is not None:
            return self.board_result.summary()
        if self.route_results:
            return " | ".join(r.summary() for r in self.route_results)
        return "; ".join(
            f"optimize {o.goal.value}: {len(o.improved)} improved, {len(o.unchanged)} unchanged"
            for o in self.optimize_reports
        )


def _nets(cmd: AICommand) -> list[str]:
    out: list[str] = []
    for t in cmd.targets:
        if isinstance(t, NetTarget):
            out.append(t.name)
        elif isinstance(t, NetGroupTarget):
            out.extend(t.names)
    return list(dict.fromkeys(out))


def _regions(cmd: AICommand, kind: SoftRegionKind) -> tuple[SoftRegion, ...]:
    regions = []
    for t in cmd.targets:
        if isinstance(t, AreaTarget):
            box = BoundingBox(
                mm_to_internal(t.x_min_mm),
                mm_to_internal(t.y_min_mm),
                mm_to_internal(t.x_max_mm),
                mm_to_internal(t.y_max_mm),
            )
            regions.append(SoftRegion(kind, box, tuple(t.layers or ())))
    return tuple(regions)


def constraints_for(cmd: AICommand, *, widths: bool = True) -> RequestConstraints:
    """AI constraints → request constraints. Only request-level values; the rule
    engine validates them and they can never weaken a rule."""
    c = cmd.effective_constraints
    width = c.preferred_trace_width_mm or c.min_trace_width_mm
    return RequestConstraints(
        preferred_width=mm_to_internal(width) if widths and width is not None else None,
        preferred_layers=tuple(c.preferred_layers or ()),
        forbidden_layers=tuple(c.forbidden_layers or ()),
        max_vias=c.max_vias,
        minimize_vias=bool(c.minimize_vias),
        soft_regions=_regions(cmd, SoftRegionKind.PREFER),
        note=(c.additional_notes or "")[:200],
    )


def request_for(cmd: AICommand, net: str, base: RouteRequest | None = None) -> RouteRequest:
    c = cmd.effective_constraints
    req = replace(base or RouteRequest(net), net=net, request_id=f"ai-{cmd.operation.value}-{net}")
    return replace(
        constraints_for(cmd).apply(req),
        allow_ripup=c.effective_allow_ripup,
        preserve_existing_routes=c.effective_preserve_existing_routes,
    )


def _rank(cmd: AICommand) -> int:
    c = cmd.effective_constraints
    return max(
        PRIORITY_RANK.get(c.priority or "normal", 0),
        CRITICALITY_RANK.get(c.criticality or "normal", 0),
    )


def _analysis(cmd: AICommand, nets: list[str]) -> list[AnalysisTarget]:
    c = cmd.effective_constraints
    out: list[AnalysisTarget] = []
    if c.max_length_mm is not None or c.target_length_mm is not None:
        out += [
            AnalysisTarget(n, c.max_length_mm, c.target_length_mm, c.length_tolerance_mm)
            for n in nets
        ]
    dp = c.differential_pair
    if dp is not None:
        out.append(
            AnalysisTarget(
                dp.positive_net,
                pair=dp.negative_net,
                pair_gap_mm=c.pair_gap_mm,
                pair_skew_mm=c.pair_skew_tolerance_mm,
            )
        )
    return out


def policy_from_commands(
    cmds: list[AICommand],
    *,
    mode: str = "accuracy",
    priorities: dict[str, int] | None = None,
    parallel_workers: int = 0,
    budget_s: float | None = None,
) -> BoardPolicy:
    """The board policy of one or more approved commands. Safe defaults: no rip-up,
    existing routes preserved. Rip-up needs every command to allow it."""
    allow = bool(cmds) and all(c.effective_constraints.effective_allow_ripup for c in cmds)
    preserve = any(c.effective_constraints.effective_preserve_existing_routes for c in cmds)
    prio = dict(priorities or {})
    pairs: list[tuple[str, str]] = []
    for cmd in cmds:
        rank = _rank(cmd)
        if rank:
            for n in _nets(cmd):
                prio[n] = max(prio.get(n, rank), rank)
        dp = cmd.effective_constraints.differential_pair
        if dp is not None:
            pairs.append((dp.positive_net, dp.negative_net))
    board_wide = RequestConstraints()
    if len(cmds) == 1 and cmds[0].operation is Operation.ROUTE_BOARD:
        board_wide = constraints_for(cmds[0], widths=False)
    return BoardPolicy(
        allow_ripup=allow,
        preserve_existing=preserve,
        ripup_user_accepted=False,
        mode=mode,
        budget_s=budget_s,
        parallel_workers=parallel_workers,
        priorities=tuple(sorted(prio.items())),
        pairs=tuple(dict.fromkeys(pairs)),
        request=board_wide,
    )


def plan_from_command(
    cmd: AICommand,
    base: RouteRequest | None = None,
    *,
    mode: str = "accuracy",
    priorities: dict[str, int] | None = None,
    parallel_workers: int = 0,
) -> ExecutionPlan:
    if cmd.operation.category is not OperationCategory.ROUTING:
        raise BridgeError(f"{cmd.operation.value} is not a routing operation")
    from pcbrouter.ai.capabilities import unsupported

    bad = unsupported(cmd.operation, cmd.effective_constraints)
    if bad:  # the validator already rejects these; never run them anyway
        raise BridgeError(
            "unsupported constraint(s): " + "; ".join(f"{n} ({fc.how})" for n, fc in bad)
        )
    nets = _nets(cmd)
    op = cmd.operation
    kw: dict[str, Any] = {
        "mode": mode,
        "priorities": priorities,
        "parallel_workers": parallel_workers,
    }
    if op is Operation.ROUTE_NET:
        if not nets:
            raise BridgeError("route_net needs a net target")
        from pcbrouter.routing.presets import RouteMode, adjust_request

        requests = [
            request_for(cmd, n, adjust_request(RouteRequest(n), RouteMode(mode))) for n in nets
        ]
        return ExecutionPlan("route_nets", requests, nets, analysis=_analysis(cmd, nets))
    if op is Operation.ROUTE_GROUP:
        if not nets:
            raise BridgeError("route_group needs net targets")
        cons = constraints_for(cmd)
        return ExecutionPlan(
            "route_board",
            [request_for(cmd, n, base) for n in nets],
            nets,
            policy=policy_from_commands([cmd], **kw),
            constraints={n: cons for n in nets},
            analysis=_analysis(cmd, nets),
        )
    if op is Operation.ROUTE_BOARD:
        if not any(isinstance(t, BoardTarget) for t in cmd.targets) and cmd.targets:
            raise BridgeError("route_board targets the board")
        return ExecutionPlan(
            "route_board",
            [],
            [],
            policy=policy_from_commands([cmd], **kw),
            analysis=_analysis(cmd, []),
        )
    from pcbrouter.routing.optimize import OptimizeGoal

    if not nets:
        raise BridgeError(f"{op.value} needs net targets")
    c = cmd.effective_constraints
    if op is Operation.REDUCE_VIAS or c.minimize_vias:
        goal = OptimizeGoal.FEWER_VIAS
    elif c.min_clearance_mm is not None:
        goal = OptimizeGoal.MORE_CLEARANCE
    else:  # "make it cleaner" defaults to fewer bends; shorter is a separate goal
        goal = OptimizeGoal.FEWER_BENDS
    return ExecutionPlan("optimize", nets=nets, goal=goal, analysis=_analysis(cmd, nets))


def plan_from_commands(
    cmds: list[AICommand],
    *,
    mode: str = "accuracy",
    priorities: dict[str, int] | None = None,
    parallel_workers: int = 0,
) -> ExecutionPlan:
    """A bounded batch plan (BATCH_APPROVAL): the approved route_net/route_group
    commands become one board-routing job over exactly those nets."""
    from pcbrouter.ai.capabilities import unsupported

    requests: list[RouteRequest] = []
    constraints: dict[str, RequestConstraints] = {}
    analysis: list[AnalysisTarget] = []
    for cmd in cmds:
        if cmd.operation not in (Operation.ROUTE_NET, Operation.ROUTE_GROUP):
            raise BridgeError(f"{cmd.operation.value} cannot be part of a batch routing plan")
        bad = unsupported(cmd.operation, cmd.effective_constraints)
        if bad:
            raise BridgeError(
                "unsupported constraint(s): " + "; ".join(f"{n} ({fc.how})" for n, fc in bad)
            )
        cons = constraints_for(cmd)
        for n in _nets(cmd):
            requests.append(request_for(cmd, n))
            constraints[n] = cons
        analysis += _analysis(cmd, _nets(cmd))
    if not requests:
        raise BridgeError("the plan has no nets to route")
    nets = list(dict.fromkeys(r.net for r in requests))
    policy = policy_from_commands(
        cmds, mode=mode, priorities=priorities, parallel_workers=parallel_workers
    )
    return ExecutionPlan(
        "route_board", requests, nets, policy=policy, constraints=constraints, analysis=analysis
    )


def execute_plan(
    plan: ExecutionPlan,
    working: WorkingBoard,
    compute: ComputeManager | None = None,
    cancel: threading.Event | None = None,
    *,
    router_factory: Callable[[Any], Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ExecutionOutcome:
    """Run the router(s). Never commits: results are proposals for user review
    (optimisations run on a fork and come back as a report).

    ``router_factory`` (engine → Router) lets the routing worker process supply its
    own backend selection; default: :func:`router_for` with ``compute``."""
    from pcbrouter.routing.backend import router_for

    if router_factory is None:
        router_factory = lambda engine: router_for(engine, compute)  # noqa: E731
    out = ExecutionOutcome(plan)
    if plan.kind == "route_nets":
        for i, req in enumerate(plan.requests):
            if cancel is not None and cancel.is_set():
                break
            if progress is not None:
                progress(
                    {
                        "phase": "ROUTING",
                        "net": req.net,
                        "index": i + 1,
                        "total_nets": len(plan.requests),
                        "completed_nets": i,
                    }
                )
            out.route_results.append(router_factory(working.engine).route_net(req, cancel=cancel))
        if plan.analysis:
            fork = working.fork()
            for r in out.route_results:
                if r.best is not None:
                    fork.commit_proposals([r.best.proposal], "analysis", validate=False)
            out.analysis_lines = analysis_report(plan.analysis, fork.board)
        return out
    if plan.kind == "route_board":
        from pcbrouter.routing.board_router import BoardRouter, BoardRoutingControl, make_plan

        policy = plan.policy or BoardPolicy()
        settings: BoardRouterSettings = policy.settings()
        out.policy_lines = describe_settings(settings, policy)
        board_plan = make_plan(working, settings, plan.nets or None)
        board_plan.tasks = [
            (
                replace(
                    t,
                    request=replace(
                        plan.constraints[t.net].apply(t.request or settings.base_request),
                        net=t.net,
                        candidates=1,
                    ),
                )
                if t.net in plan.constraints
                else t
            )
            for t in board_plan.tasks
        ]
        control = BoardRoutingControl()
        if cancel is not None:
            control.cancel_event = cancel  # cancellation reaches every net and search
        router = BoardRouter(working, settings, router_factory=router_factory)
        out.board_result = router.run(board_plan, control, progress)
        out.analysis_lines = analysis_report(plan.analysis, out.board_result.final_board)
        return out
    from pcbrouter.routing.board_router import BoardRoutingControl
    from pcbrouter.routing.optimize import optimize_nets

    control = BoardRoutingControl()
    if cancel is not None:
        control.cancel_event = cancel
    fork = working.fork()
    out.optimize_reports.append(optimize_nets(fork, plan.nets, plan.goal, control=control))
    out.optimized_board = fork.board
    out.analysis_lines = analysis_report(plan.analysis, fork.board)
    return out


def analysis_report(targets: list[AnalysisTarget], board: Board) -> list[str]:
    """Measure ANALYSIS_ONLY targets on ``board``. Reports, never enforces."""
    from pcbrouter.routing.diffpair import pair_metrics

    lines: list[str] = []
    for a in targets:
        if a.pair is not None:
            m = pair_metrics(board, a.net, a.pair)
            if not m.length_p_nm or not m.length_n_nm:
                lines.append(f"Pair {a.net}/{a.pair}: not both routed; nothing measured")
                continue
            skew = m.skew_nm / 1e6
            text = f"Pair {a.net}/{a.pair}: skew {skew:.2f} mm"
            if a.pair_skew_mm is not None:
                text += " (within" if skew <= a.pair_skew_mm else " (OUTSIDE"
                text += f" the {a.pair_skew_mm} mm tolerance)"
            if m.min_gap_nm is not None:
                text += f", gap {m.min_gap_nm / 1e6:.2f}–{(m.mean_gap_nm or 0) / 1e6:.2f} mm"
                if a.pair_gap_mm is not None:
                    text += f" (target {a.pair_gap_mm} mm, not controlled)"
            lines.append(text + "; impedance not computed. Analysis only.")
            continue
        length = sum(t.length for t in board.tracks if t.net_name == a.net) / 1e6
        if not length:
            lines.append(f"{a.net}: not routed; length not measured")
            continue
        text = f"{a.net}: routed length {length:.2f} mm"
        if a.max_length_mm is not None:
            text += " (within" if length <= a.max_length_mm else " (EXCEEDS"
            text += f" the {a.max_length_mm} mm maximum)"
        if a.target_length_mm is not None:
            tol = a.tolerance_mm or 0.0
            ok = abs(length - a.target_length_mm) <= tol
            text += f"; target {a.target_length_mm} ± {tol} mm: " + ("met" if ok else "NOT met")
        lines.append(text + ". Analysis only: the router does not tune length.")
    return lines


def router_facts(outcome: ExecutionOutcome) -> list[dict[str, Any]]:
    """Structured ROUTER RESULT facts for the planner (deterministic, no geometry)."""
    facts: list[dict[str, Any]] = []
    for r in outcome.route_results:
        fact: dict[str, Any] = {
            "net": r.net,
            "status": r.status.value,
            "reason": r.reason.value if r.reason else None,
            "message": r.message[:200],
        }
        if r.best is not None:
            fact["best"] = r.best.score.to_dict()
            fact["candidates"] = len(r.candidates)
        if r.blockers:
            fact["blocking_cells"] = dict(sorted(r.blockers.items())[:6])
        facts.append(fact)
    if outcome.board_result is not None:
        b = outcome.board_result
        facts.append(
            {
                "board": b.status.value,
                "metrics": b.metrics.to_dict(),
                "failed": {
                    n: (o.reason.value if o.reason else o.status.value)
                    for n, o in b.outcomes.items()
                    if o.status.value != "SUCCESS"
                },
            }
        )
    if outcome.policy_lines:
        facts.append({"policy": outcome.policy_lines})
    if outcome.analysis_lines:
        facts.append({"analysis_only": outcome.analysis_lines})
    for rep in outcome.optimize_reports:
        facts.append(
            {
                "optimize": rep.goal.value,
                "improved": sorted(rep.improved),
                "unchanged": rep.unchanged,
                "skipped": rep.skipped,
            }
        )
    return facts
