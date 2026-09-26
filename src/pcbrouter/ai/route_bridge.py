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
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.routing.request import RouteRequest, SoftRegion, SoftRegionKind

if TYPE_CHECKING:
    from pcbrouter.compute.manager import ComputeManager
    from pcbrouter.routing.board_router import BoardRoutingResult
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


@dataclass
class ExecutionPlan:
    kind: str  # "route_nets" | "route_board" | "optimize"
    requests: list[RouteRequest] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    goal: Any = None  # OptimizeGoal
    notes: list[str] = field(default_factory=list)


@dataclass
class ExecutionOutcome:
    plan: ExecutionPlan
    route_results: list[RouteResult] = field(default_factory=list)
    board_result: BoardRoutingResult | None = None
    optimize_reports: list[OptimizeReport] = field(default_factory=list)
    #: the fork's board after an optimisation plan (applied by the user as one commit)
    optimized_board: Any = None

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


def request_for(cmd: AICommand, net: str, base: RouteRequest | None = None) -> RouteRequest:
    """AI constraints → a RouteRequest. Only request-level preferences: the rule
    engine validates them and they can never weaken a rule."""
    c = cmd.effective_constraints
    req = base or RouteRequest(net)
    width = c.preferred_trace_width_mm or c.min_trace_width_mm
    return replace(
        req,
        net=net,
        request_id=f"ai-{cmd.operation.value}-{net}",
        preferred_width=mm_to_internal(width) if width is not None else req.preferred_width,
        preferred_layers=tuple(c.preferred_layers or ()),
        forbidden_layers=tuple(c.forbidden_layers or ()),
        max_vias=c.max_vias if c.max_vias is not None else req.max_vias,
        minimize_vias=bool(c.minimize_vias) or req.minimize_vias,
        allow_ripup=c.effective_allow_ripup,
        preserve_existing_routes=c.effective_preserve_existing_routes,
        soft_regions=req.soft_regions + _regions(cmd, SoftRegionKind.PREFER),
        constraints_note=(c.additional_notes or "")[:200],
    )


def plan_from_command(cmd: AICommand, base: RouteRequest | None = None) -> ExecutionPlan:
    if cmd.operation.category is not OperationCategory.ROUTING:
        raise BridgeError(f"{cmd.operation.value} is not a routing operation")
    nets = _nets(cmd)
    op = cmd.operation
    if op is Operation.ROUTE_NET:
        if not nets:
            raise BridgeError("route_net needs a net target")
        return ExecutionPlan("route_nets", [request_for(cmd, n, base) for n in nets], nets)
    if op is Operation.ROUTE_GROUP:
        if not nets:
            raise BridgeError("route_group needs net targets")
        return ExecutionPlan("route_board", [request_for(cmd, n, base) for n in nets], nets)
    if op is Operation.ROUTE_BOARD:
        if not any(isinstance(t, BoardTarget) for t in cmd.targets) and cmd.targets:
            raise BridgeError("route_board targets the board")
        return ExecutionPlan("route_board", [], [])
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
    return ExecutionPlan("optimize", nets=nets, goal=goal)


def plan_from_commands(cmds: list[AICommand]) -> ExecutionPlan:
    """A bounded batch plan (BATCH_APPROVAL): the approved route_net/route_group
    commands become one board-routing job over exactly those nets."""
    requests: list[RouteRequest] = []
    for cmd in cmds:
        if cmd.operation not in (Operation.ROUTE_NET, Operation.ROUTE_GROUP):
            raise BridgeError(f"{cmd.operation.value} cannot be part of a batch routing plan")
        requests += [request_for(cmd, n) for n in _nets(cmd)]
    if not requests:
        raise BridgeError("the plan has no nets to route")
    nets = list(dict.fromkeys(r.net for r in requests))
    return ExecutionPlan("route_board", requests, nets)


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
        return out
    if plan.kind == "route_board":
        from pcbrouter.routing.board_router import (
            BoardRouter,
            BoardRouterSettings,
            BoardRoutingControl,
            make_plan,
        )

        settings = BoardRouterSettings()
        board_plan = make_plan(working, settings, plan.nets or None)
        by_net = {r.net: r for r in plan.requests}
        board_plan.tasks = [
            replace(t, request=replace(by_net[t.net], candidates=1)) if t.net in by_net else t
            for t in board_plan.tasks
        ]
        control = BoardRoutingControl()
        if cancel is not None:
            control.cancel_event = cancel  # cancellation reaches every net and search
        router = BoardRouter(working, settings, router_factory=router_factory)
        out.board_result = router.run(board_plan, control, progress)
        return out
    from pcbrouter.routing.board_router import BoardRoutingControl
    from pcbrouter.routing.optimize import optimize_nets

    control = BoardRoutingControl()
    if cancel is not None:
        control.cancel_event = cancel
    fork = working.fork()
    out.optimize_reports.append(optimize_nets(fork, plan.nets, plan.goal, control=control))
    out.optimized_board = fork.board
    return out


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
