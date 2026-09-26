"""Routing commands (Stage 4+). Every route and every commit goes through the bus.

* :class:`RouteNetCommand` runs the deterministic router and returns candidates.
  It changes nothing.
* :class:`AcceptRouteCommand` commits one validated candidate to the *working
  board* (never the source file) and records an undoable history entry.
* :class:`WorkingUndoAction` makes commits part of the global undo/redo stack.

``modifies_board`` stays False: these commands change the in-memory working
board only. Writing a KiCad file is a separate, explicit export (Stage 9).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pcbrouter.commands.base import BaseCommand, CommandContext, CommandResult
from pcbrouter.history.history import UndoableAction
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import RouteCandidate
from pcbrouter.routing.working_board import Commit, CommitError, Provenance, WorkingBoard

if TYPE_CHECKING:
    from pcbrouter.jobs.protocol import OptimizationPlan


class WorkingUndoAction(UndoableAction):
    """History entry for a working-board commit (apply = redo, revert = undo)."""

    def __init__(self, working: WorkingBoard, commit: Commit) -> None:
        self.working = working
        self.commit = commit

    @property
    def label(self) -> str:
        return self.commit.label

    @property
    def kind(self) -> str:
        return "working_board"

    @property
    def metadata(self) -> Mapping[str, Any]:
        return {
            "commit": self.commit.commit_id,
            "diff": self.commit.diff_summary(),
            "nets": self.commit.nets,
        }

    def apply(self) -> None:
        self.working.redo(self.commit)

    def revert(self) -> None:
        if self.working.commits and self.working.commits[-1] is self.commit:
            self.working.undo()
        else:
            raise CommitError("working board history is out of order")


def _working(ctx: CommandContext) -> WorkingBoard | None:
    return ctx.project.working


@dataclass
class RouteNetCommand(BaseCommand):
    request: RouteRequest
    cancel: threading.Event | None = None
    name: ClassVar[str] = "route_net"

    def execute(self, ctx: CommandContext) -> CommandResult:
        engine = ctx.project.engine
        if engine is None:
            return CommandResult.fail("Open a board first.")
        from pcbrouter.routing.backend import router_for

        router = router_for(engine, ctx.compute)
        result = router.route_net(self.request, cancel=self.cancel)
        return CommandResult(result.success, result.summary(), result)

    def describe(self) -> str:
        return f"route net {self.request.net}"


@dataclass
class AcceptRouteCommand(BaseCommand):
    candidate: RouteCandidate
    label: str = ""
    provenance: Provenance = Provenance.USER_ACCEPTED
    #: generated copper replaced by this route (local reroute); validated together
    remove_ids: tuple[str, ...] = ()
    name: ClassVar[str] = "accept_route"

    def execute(self, ctx: CommandContext) -> CommandResult:
        working = _working(ctx)
        if working is None:
            return CommandResult.fail("Open a board first.")
        if not self.candidate.legal:
            return CommandResult.fail("Only validated candidates can be accepted.")
        label = self.label or f"Route accepted: {self.candidate.proposal.net}"
        try:
            commit = working.commit_proposals(
                [self.candidate.proposal],
                label,
                self.provenance,
                remove_ids=self.remove_ids,
                metadata={
                    "score": self.candidate.score.to_dict(),
                    "proposal": self.candidate.proposal.proposal_id,
                },
            )
        except CommitError as exc:
            return CommandResult.fail(str(exc))
        ctx.history.push(WorkingUndoAction(working, commit), already_applied=True)
        return CommandResult.ok(f"{label} ({commit.diff_summary()})", commit)

    def describe(self) -> str:
        return f"accept route {self.candidate.proposal.net}"


@dataclass
class ResetWorkingBoardCommand(BaseCommand):
    name: ClassVar[str] = "reset_working_board"

    def execute(self, ctx: CommandContext) -> CommandResult:
        working = _working(ctx)
        if working is None:
            return CommandResult.fail("Open a board first.")
        n = len(working.commits)
        working.reset()
        ctx.history.clear()
        return CommandResult.ok(f"Working board reset to the source ({n} commit(s) removed).")


@dataclass
class RouteBoardCommand(BaseCommand):
    """Run the board router on a fork of the working board (changes nothing)."""

    settings: Any = None  # BoardRouterSettings
    nets: list[str] | None = None
    control: Any = None  # BoardRoutingControl
    progress: Any = None
    name: ClassVar[str] = "route_board"

    def execute(self, ctx: CommandContext) -> CommandResult:
        from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, make_plan

        working = _working(ctx)
        if working is None:
            return CommandResult.fail("Open a board first.")
        settings = self.settings or BoardRouterSettings()
        plan = make_plan(working, settings, self.nets)
        result = BoardRouter(working, settings).run(plan, self.control, self.progress)
        return CommandResult(True, result.summary(), result)


@dataclass
class AcceptBoardRoutingCommand(BaseCommand):
    """Commit a board-routing batch (all nets, or a subset) to the working board."""

    result: Any  # BoardRoutingResult
    nets: set[str] | None = None
    name: ClassVar[str] = "accept_board_routing"

    def execute(self, ctx: CommandContext) -> CommandResult:
        working = _working(ctx)
        if working is None:
            return CommandResult.fail("Open a board first.")
        if working.board.fingerprint != self.result.base_board.fingerprint:
            return CommandResult.fail(
                "The working board changed since this routing job started; route again."
            )
        tracks, vias, removed = self.result.objects_for(self.nets)
        if not tracks and not vias and not removed:
            return CommandResult.fail("Nothing to accept.")
        which = "all nets" if self.nets is None else ", ".join(sorted(self.nets))
        try:
            commit = working.commit_objects(
                tracks,
                vias,
                removed,
                f"Board routing accepted ({which})",
                Provenance.ROUTER_GENERATED,
                metadata={"metrics": self.result.metrics.to_dict()},
            )
        except CommitError as exc:
            return CommandResult.fail(str(exc))
        ctx.history.push(WorkingUndoAction(working, commit), already_applied=True)
        return CommandResult.ok(f"{commit.label}: {commit.diff_summary()}", commit)


def plan_optimization(
    working: WorkingBoard, net: str, goal: Any, control: Any = None
) -> OptimizationPlan:
    """Compute a net optimisation on a fork (never touches ``working``). Used by
    :class:`OptimizeNetCommand` and by the routing worker process."""
    from pcbrouter.jobs.protocol import OptimizationPlan
    from pcbrouter.routing.optimize import optimize_nets

    fork = working.fork()
    report = optimize_nets(fork, [net], goal, control=control)
    base = {t.id for t in working.board.tracks} | {v.id for v in working.board.vias}
    final = {t.id for t in fork.board.tracks} | {v.id for v in fork.board.vias}
    improved = net in report.improved
    return OptimizationPlan(
        working.board.fingerprint,
        net,
        goal,
        [t for t in fork.board.tracks if t.id not in base] if improved else [],
        [v for v in fork.board.vias if v.id not in base] if improved else [],
        sorted(base - final) if improved else [],
        report,
        improved,
    )


def apply_optimization(ctx: CommandContext, plan: OptimizationPlan) -> CommandResult:
    """Apply a computed optimisation as one validated, undoable commit. ``plan.net``
    is one net, or a comma-separated label for a batch (AI tweak)."""
    working = _working(ctx)
    if working is None:
        return CommandResult.fail("Open a board first.")
    report, net, goal = plan.report, plan.net, plan.goal
    what = goal.value.replace("_", " ")
    if net in report.skipped:
        return CommandResult.fail(f"{net}: {report.skipped[net]}")
    if not plan.improved:
        return CommandResult.ok(f"{net}: no {what} improvement found; route kept", report)
    if working.board.fingerprint != plan.base_fingerprint:
        return CommandResult.fail(
            f"{net}: the board changed while the optimisation ran; run it again"
        )
    if net in report.improved:
        before, after = report.improved[net]
        metadata: dict[str, Any] = {"before": before.to_dict(), "after": after.to_dict()}
        summary = f"{before.to_dict()} → {after.to_dict()}"
    else:
        metadata = {
            "nets": {
                n: {"before": b.to_dict(), "after": a.to_dict()}
                for n, (b, a) in sorted(report.improved.items())
            }
        }
        summary = f"{len(report.improved)} net(s) improved"
    try:
        commit = working.commit_objects(
            plan.add_tracks,
            plan.add_vias,
            plan.remove_ids,
            f"Optimize {net}: {what}",
            Provenance.OPTIMIZER,
            metadata=metadata,
        )
    except CommitError as exc:
        return CommandResult.fail(str(exc))
    ctx.history.push(WorkingUndoAction(working, commit), already_applied=True)
    return CommandResult.ok(f"{commit.label}: {summary}", report)


@dataclass
class ApplyOptimizationCommand(BaseCommand):
    """Apply an optimisation computed by the routing worker (validated commit)."""

    plan: Any  # OptimizationPlan
    name: ClassVar[str] = "apply_optimization"

    def execute(self, ctx: CommandContext) -> CommandResult:
        return apply_optimization(ctx, self.plan)

    def describe(self) -> str:
        return f"apply optimisation of {self.plan.net}"


@dataclass
class OptimizeNetCommand(BaseCommand):
    """Deterministic tweak of one net's generated copper (one undoable commit)."""

    net: str
    goal: Any  # OptimizeGoal
    name: ClassVar[str] = "optimize_net"

    def execute(self, ctx: CommandContext) -> CommandResult:
        working = _working(ctx)
        if working is None:
            return CommandResult.fail("Open a board first.")
        return apply_optimization(ctx, plan_optimization(working, self.net, self.goal))
