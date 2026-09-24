"""CPU autorouter v1 (Stage 4): single-net routing between copper groups.

Pipeline for one request::

    RouteRequest ──normalise (rules)──▶ SearchGrid (occupancy, via mask, costs)
        ──A* per connection──▶ grid path ──simplify (octilinear, exact checks)──▶
        segments + vias ──per-element exact validation (repair on failure)──▶
        RouteProposal ──validate_route (continuity, max vias)──▶ RouteCandidate

A net with N copper groups needs N-1 connections: the router grows the connected
set from one group and each search ends on any cell of any remaining group (pad,
track, via or zone copper of the same net). Up to ``request.candidates``
alternatives are produced by penalising cells used by earlier candidates; every
candidate must pass the Stage 3 validator or it is not offered.

The router never modifies the board: it returns proposals. Committing is the
working board's job (:mod:`pcbrouter.routing.working_board`).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import MappingProxyType

import numpy as np
import numpy.typing as npt

from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm, format_mm
from pcbrouter.geometry.board import ItemKind
from pcbrouter.routing.collision import CollisionResult, ValidationStatus
from pcbrouter.routing.connectivity import net_connectivity
from pcbrouter.routing.occupancy import CellState, GridSpec
from pcbrouter.routing.path.simplify import (
    _direction,
    collapse_collinear,
    runs_from_path,
    shortcut,
    snap_end,
)
from pcbrouter.routing.proposal import ProposalSource, RouteProposal, RouteSegment, RouteVia
from pcbrouter.routing.request import (
    NormalisedRequest,
    RouteRequest,
    RouteRequestError,
    RuleUnknownError,
    SoftRegionKind,
    normalise,
)
from pcbrouter.routing.result import (
    FailureReason,
    RouteCandidate,
    RouteResult,
    RouteScore,
    RouteStatus,
)
from pcbrouter.routing.search.astar import SearchOutcome, SearchProblem, SearchStatus, search
from pcbrouter.routing.search.grid import SearchGrid, compile_grid

log = logging.getLogger(__name__)

ROUTER_VERSION = "1.0.0"
MAX_REPAIRS = 6
WINDOW_MARGIN_NM: Nm = 3_000_000
DIAGNOSE_NODE_LIMIT = 300_000

#: (layer, grid spec) -> extra cost per cell (fraction of step), e.g. congestion
PenaltyProvider = Callable[[str, GridSpec], npt.NDArray[np.float64] | None]
#: a search backend with the same contract as :func:`search`
SearchFn = Callable[..., SearchOutcome]


@dataclass
class _Connection:
    segments: list[RouteSegment]
    vias: list[RouteVia]
    cells: list[tuple[int, int]]
    cost: float


@dataclass
class _Attempt:
    connections: list[_Connection] = field(default_factory=list)
    failure: FailureReason | None = None
    message: str = ""
    total: int = 0


class Router:
    def __init__(
        self,
        engine: BoardEngine,
        search_fn: SearchFn | None = None,
        backend_name: str = "cpu",
    ) -> None:
        self.engine = engine
        self.search_fn: SearchFn = search_fn or search
        self.backend_name = backend_name

    # ------------------------------------------------------------ public API
    def route_net(
        self,
        request: RouteRequest,
        *,
        cancel: threading.Event | None = None,
        penalties: PenaltyProvider | None = None,
        avoid_uids: frozenset[str] = frozenset(),
    ) -> RouteResult:
        """Route ``request.net``. Never raises for routing problems: see status."""
        t0 = time.perf_counter()
        result = RouteResult(request.request_id, request.net, RouteStatus.NO_ROUTE)
        result.metrics.backend = self.backend_name
        try:
            norm = normalise(self.engine, request)
        except RouteRequestError as exc:
            result.status, result.message = RouteStatus.INVALID_REQUEST, str(exc)
            return self._done(result, t0)
        except RuleUnknownError as exc:
            result.status, result.reason = RouteStatus.RULE_UNKNOWN, FailureReason.RULE_UNKNOWN
            result.message = str(exc)
            return self._done(result, t0)
        result.width = norm.width
        result.details += list(norm.notes)
        try:
            self._route(norm, result, cancel, penalties, avoid_uids)
        except Exception as exc:  # reported, never hidden; the board is untouched
            log.exception("router.internal_error net=%s", request.net)
            result.status, result.message = RouteStatus.INTERNAL_ERROR, repr(exc)
            result.candidates = []
        return self._done(result, t0)

    # ------------------------------------------------------------ core
    def _route(
        self,
        norm: NormalisedRequest,
        result: RouteResult,
        cancel: threading.Event | None,
        penalties: PenaltyProvider | None,
        avoid_uids: frozenset[str],
    ) -> None:
        geo = self.engine.geometry
        conn = net_connectivity(geo, norm.net)
        groups = [list(m) for m in conn.group_members]
        if len(conn.pad_uids) < 2 or len(groups) < 2:
            result.status = RouteStatus.ALREADY_CONNECTED
            result.message = (
                "fewer than two pads: nothing to connect"
                if len(conn.pad_uids) < 2
                else "already fully connected"
            )
            return
        groups = self._order_groups(groups, norm.request)
        result.connections_total = len(groups) - 1
        windows: list[BoundingBox | None] = [self._window(groups)]
        if windows[0] is not None:
            windows.append(None)
        for window in windows:
            grid = compile_grid(
                self.engine,
                norm.net,
                norm.layers,
                norm.width,
                norm.via_diameter if norm.vias_allowed else None,
                norm.request.grid_resolution,
                window,
            )
            self._apply_costs(grid, norm, penalties, avoid_uids)
            result.metrics.grid_cells = grid.n * len(grid.layers)
            if not grid.rules_complete:
                result.details.append("some rules are unknown: grid may be optimistic")
            done = self._candidates(grid, norm, groups, result, cancel)
            if done or result.status in (RouteStatus.CANCELLED, RouteStatus.TIMEOUT):
                return
            if window is not None:
                result.details.append(
                    "no route inside the net's neighbourhood; trying the whole board"
                )

    def _candidates(
        self,
        grid: SearchGrid,
        norm: NormalisedRequest,
        groups: list[list[str]],
        result: RouteResult,
        cancel: threading.Event | None,
    ) -> bool:
        seen: set[tuple[object, ...]] = set()
        for k in range(norm.request.candidates):
            attempt = self._attempt(grid, norm, groups, result, cancel)
            if attempt.failure is not None and not attempt.connections:
                if k == 0:
                    self._fail(result, attempt, norm, grid, groups, cancel)
                break
            proposal = self._proposal(norm, attempt, k)
            key = (proposal.segments, proposal.vias)
            if key in seen:
                self._penalise(grid, attempt, norm)
                continue
            seen.add(key)
            validation = self.engine.validator.validate_route(proposal)
            label = "Best" if not result.candidates else f"Alternative {len(result.candidates)}"
            cand = RouteCandidate(
                label,
                proposal,
                validation,
                self._score(proposal, validation, attempt, grid),
                connections=len(attempt.connections),
            )
            if attempt.failure is not None:
                cand.warnings.append(f"partial: {attempt.message}")
            if validation.legal:
                result.candidates.append(cand)
                result.connections_routed = max(result.connections_routed, len(attempt.connections))
            elif validation.status is ValidationStatus.RULE_UNKNOWN:
                result.status, result.reason = RouteStatus.RULE_UNKNOWN, FailureReason.RULE_UNKNOWN
                result.message = "route found, but a rule it depends on is unknown"
                result.details += validation.messages[:10]
                return True
            else:  # should not happen: every element was validated; keep it visible
                result.details += validation.messages[:10]
            if attempt.failure is not None:
                break  # partial: no point in alternatives
            self._penalise(grid, attempt, norm)
        if not result.candidates:
            return False
        best = result.candidates[0]
        full = best.connections == result.connections_total
        result.status = RouteStatus.SUCCESS if full else RouteStatus.PARTIAL
        if not full and result.reason is None:
            result.reason = FailureReason.NO_PATH
        return True

    def _attempt(
        self,
        grid: SearchGrid,
        norm: NormalisedRequest,
        groups: list[list[str]],
        result: RouteResult,
        cancel: threading.Event | None,
    ) -> _Attempt:
        nl = len(grid.layers)
        attempt = _Attempt(total=len(groups) - 1)
        connected = [groups[0]]
        remaining = list(groups[1:])
        extra_sources: list[list[int]] = [[] for _ in range(nl)]
        vias_used = 0
        while remaining:
            sources = self._cells_of(grid, [u for g in connected for u in g])
            for li in range(nl):
                if extra_sources[li]:
                    sources[li] = np.unique(
                        np.concatenate([sources[li], np.asarray(extra_sources[li])])
                    )
            target_masks = [np.zeros((grid.ny, grid.nx), dtype=np.bool_) for _ in range(nl)]
            group_cells = [self._cells_of(grid, g) for g in remaining]
            for gc in group_cells:
                for li in range(nl):
                    target_masks[li].reshape(-1)[gc[li]] = True
            if not any(
                int(np.count_nonzero(grid.passable[li].reshape(-1)[sources[li]]))
                for li in range(nl)
            ):
                attempt.failure, attempt.message = (
                    FailureReason.NO_ESCAPE,
                    "source copper is enclosed",
                )
                return attempt
            limit = None if norm.max_vias is None else max(0, norm.max_vias - vias_used)
            problem = SearchProblem(
                grid,
                sources,
                target_masks,
                norm.request.cost,
                self._layer_factors(norm, grid),
                self._via_cost(norm),
                limit,
                octilinear=norm.request.routing_style.value == "45",
                vias_enabled=norm.vias_allowed and limit != 0,
            )
            connection = None
            for _repair in range(MAX_REPAIRS + 1):
                outcome = self.search_fn(
                    problem,
                    node_limit=norm.request.node_limit,
                    time_limit_s=norm.request.time_limit_s,
                    cancel=cancel,
                )
                result.metrics.expanded_nodes += outcome.expanded
                result.metrics.searches += 1
                if outcome.status is not SearchStatus.FOUND:
                    attempt.failure, attempt.message = self._search_failure(outcome, result)
                    return attempt
                connection, bad = self._geometry(grid, norm, outcome)
                if not bad:
                    break
                result.metrics.repairs += 1
                for li, cells in bad:
                    if li < 0:
                        grid.block_via(cells)
                    else:
                        grid.block(li, cells)
                connection = None
            if connection is None:
                attempt.failure = FailureReason.VALIDATION
                attempt.message = "search paths kept failing exact validation"
                return attempt
            attempt.connections.append(connection)
            vias_used += len(connection.vias)
            end_li, end_idx = connection.cells[-1]
            reached = next(
                (i for i, gc in enumerate(group_cells) if end_idx in set(gc[end_li].tolist())), 0
            )
            connected.append(remaining.pop(reached))
            for seg in connection.segments:
                li = grid.layers.index(seg.layer)
                for p in (seg.start, seg.end):
                    idx = grid.index_of(p)
                    if idx is not None and grid.center(idx) == p:
                        extra_sources[li].append(idx)
            for via in connection.vias:
                idx = grid.index_of(via.position)
                if idx is not None:
                    for li in range(nl):
                        extra_sources[li].append(idx)
        return attempt

    # ------------------------------------------------------------ geometry
    def _geometry(
        self, grid: SearchGrid, norm: NormalisedRequest, outcome: SearchOutcome
    ) -> tuple[_Connection, list[tuple[int, npt.NDArray[np.int64]]]]:
        """Path → validated segments/vias. Returns cells to block when invalid."""
        validator = self.engine.validator
        memo: dict[tuple[str, Point, Point], bool] = {}

        def check(layer: str, a: Point, b: Point) -> bool:
            key = (layer, a, b) if (a.x, a.y) <= (b.x, b.y) else (layer, b, a)
            if key not in memo:
                memo[key] = validator.validate_segment(norm.net, layer, a, b, norm.width).legal
            return memo[key]

        runs = runs_from_path(grid, outcome.path)
        start_item = self._pad_at(grid, outcome.path[0])
        end_item = self._pad_at(grid, outcome.path[-1])
        segments: list[RouteSegment] = []
        vias: list[RouteVia] = []
        bad: list[tuple[int, npt.NDArray[np.int64]]] = []
        for k, run in enumerate(runs):
            pts = shortcut(run.points, run.layer, check)
            if k == 0 and start_item is not None:
                pts = snap_end(pts, start_item, run.layer, check, at_start=True)
            if k == len(runs) - 1 and end_item is not None:
                pts = snap_end(pts, end_item, run.layer, check, at_start=False)
            pts = collapse_collinear(pts)
            li = grid.layers.index(run.layer)
            for a, b in itertools.pairwise(pts):
                res = validator.validate_segment(norm.net, run.layer, a, b, norm.width)
                if not res.legal:
                    bad.append((li, self._blocking_cells(grid, res, a, b, norm)))
                segments.append(RouteSegment(a, b, run.layer, norm.width))
            if k < len(runs) - 1:
                pos = run.points[-1]
                assert norm.via_diameter is not None and norm.via_drill is not None
                cl = self.engine.geometry.copper_layers
                res = validator.validate_via(
                    norm.net, pos, cl[0], cl[-1], norm.via_diameter, norm.via_drill
                )
                if not res.legal:
                    idx = grid.index_of(pos)
                    if idx is not None:
                        bad.append((-1, np.asarray([idx], dtype=np.int64)))  # via-only
                vias.append(RouteVia(pos, cl[0], cl[-1], norm.via_diameter, norm.via_drill))
        return _Connection(segments, vias, outcome.path, outcome.cost), bad

    def _blocking_cells(
        self, grid: SearchGrid, res: CollisionResult, a: Point, b: Point, norm: NormalisedRequest
    ) -> npt.NDArray[np.int64]:
        """Cells to make impassable after an exact-validation failure: the region of
        each colliding object (grown by the track half-width + required clearance),
        or the neighbourhood of the offending segment."""
        geo = self.engine.geometry
        out: list[npt.NDArray[np.int64]] = []
        for c in res.collisions:
            grow = norm.width / 2 + (c.required or 0) + grid.spec.cell
            item = geo.copper.get(c.object_id or "") or None
            if item is not None:
                for s in item.shapes:
                    out.append(grid.cells_within(s, grow))
            elif c.location is not None:
                from pcbrouter.geometry.shapes import circle

                out.append(grid.cells_within(circle(c.location, norm.width), grow))
        if not out:
            from pcbrouter.geometry.shapes import capsule

            out.append(grid.cells_within(capsule(a, b, 0), grid.spec.cell))
        return np.unique(np.concatenate(out)) if out else np.zeros(0, dtype=np.int64)

    def _pad_at(self, grid: SearchGrid, state: tuple[int, int]) -> Point | None:
        """Centre of the pad containing the path end (for end snapping)."""
        li, idx = state
        p = grid.center(idx)
        probe = BoundingBox(p.x, p.y, p.x, p.y)
        for item in self.engine.geometry.copper_near(grid.layers[li], probe):
            if item.kind is ItemKind.PAD and any(s.contains(p) for s in item.shapes):
                return item.bounds.center
        return None

    # ------------------------------------------------------------ helpers
    def _cells_of(self, grid: SearchGrid, uids: list[str]) -> list[npt.NDArray[np.int64]]:
        geo = self.engine.geometry
        per_layer: list[list[npt.NDArray[np.int64]]] = [[] for _ in grid.layers]
        for uid in uids:
            item = geo.copper.get(uid)
            if item is None:
                continue
            for li, layer in enumerate(grid.layers):
                if layer not in item.layers:
                    continue
                for s in item.shapes:
                    cells = grid.cells_within(s, 0.0)
                    if cells.size == 0:
                        idx = grid.index_of(item.bounds.center)
                        if idx is not None:
                            cells = np.asarray([idx], dtype=np.int64)
                    per_layer[li].append(cells)
        return [
            np.unique(np.concatenate(c)) if c else np.zeros(0, dtype=np.int64) for c in per_layer
        ]

    @staticmethod
    def _order_groups(groups: list[list[str]], request: RouteRequest) -> list[list[str]]:
        if request.source_group:
            src = set(request.source_group)
            first = [g for g in groups if src & set(g)]
            if first:
                rest = [g for g in groups if g is not first[0]]
                if request.target_group:
                    tgt = set(request.target_group)
                    rest = [g for g in rest if tgt & set(g)]
                return [first[0], *rest]
        # deterministic: largest group first (most pads), ties by uid order
        return sorted(groups, key=lambda g: (-len(g), g))

    def _window(self, groups: list[list[str]]) -> BoundingBox | None:
        geo = self.engine.geometry
        board_box = geo.board.bounds
        boxes = [geo.copper[u].bounds for g in groups for u in g if u in geo.copper]
        if not boxes or board_box is None:
            return None
        box = boxes[0]
        for b in boxes[1:]:
            box = box.union(b)
        margin = max(WINDOW_MARGIN_NM, max(box.width, box.height) // 4)
        box = box.expanded(margin)
        clipped = BoundingBox(
            max(box.min_x, board_box.min_x),
            max(box.min_y, board_box.min_y),
            min(box.max_x, board_box.max_x),
            min(box.max_y, board_box.max_y),
        )
        if clipped.width * clipped.height >= 0.8 * board_box.width * board_box.height:
            return None
        return clipped

    def _layer_factors(self, norm: NormalisedRequest, grid: SearchGrid) -> list[float]:
        f = norm.request.cost.nonpreferred_layer_factor
        return [1.0 if lay in norm.preferred_layers else f for lay in grid.layers]

    @staticmethod
    def _via_cost(norm: NormalisedRequest) -> float:
        c = norm.request.cost
        return c.via_nm * (c.minimize_vias_multiplier if norm.request.minimize_vias else 1.0)

    def _apply_costs(
        self,
        grid: SearchGrid,
        norm: NormalisedRequest,
        penalties: PenaltyProvider | None,
        avoid_uids: frozenset[str],
    ) -> None:
        cm = norm.request.cost
        for li, layer in enumerate(grid.layers):
            if penalties is not None:
                pen = penalties(layer, grid.spec)
                if pen is not None:
                    grid.penalty[li] = pen.astype(np.float64) * cm.congestion_weight
            for region in norm.request.soft_regions:
                if region.layers and layer not in region.layers:
                    continue
                from pcbrouter.geometry.shapes import rectangle

                b = region.box
                shape = rectangle(b.center, b.width, b.height)
                cells = grid.cells_within(shape, 0.0)
                if region.kind is SoftRegionKind.AVOID:
                    grid.add_penalty(li, cells, cm.corridor_avoid_factor)
                else:
                    fac = grid.factor[li]
                    if fac is None:
                        fac = np.ones((grid.ny, grid.nx), dtype=np.float64)
                        grid.factor[li] = fac
                    fac.reshape(-1)[cells] = cm.corridor_prefer_factor
        geo = self.engine.geometry
        for uid in sorted(avoid_uids):  # e.g. generated routes the caller wants avoided
            item = geo.copper.get(uid)
            if item is None:
                continue
            for li, layer in enumerate(grid.layers):
                if layer in item.layers:
                    for s in item.shapes:
                        grid.add_penalty(li, grid.cells_within(s, norm.width), cm.reuse_factor)

    def _penalise(self, grid: SearchGrid, attempt: _Attempt, norm: NormalisedRequest) -> None:
        amount = norm.request.cost.reuse_factor
        by_layer: dict[int, list[int]] = {}
        for c in attempt.connections:
            for li, idx in c.cells:
                by_layer.setdefault(li, []).append(idx)
        for li, cells in by_layer.items():
            grid.add_penalty(li, np.unique(np.asarray(cells, dtype=np.int64)), amount)

    def _proposal(self, norm: NormalisedRequest, attempt: _Attempt, k: int) -> RouteProposal:
        segs = tuple(s for c in attempt.connections for s in c.segments)
        vias = tuple(v for c in attempt.connections for v in c.vias)
        req = norm.request
        return RouteProposal(
            norm.net,
            segs,
            vias,
            ProposalSource.CPU_ROUTER,
            f"{req.request_id}-c{k}",
            MappingProxyType(
                {
                    "request_id": req.request_id,
                    "candidate": k,
                    "seed": req.seed,
                    "router_version": ROUTER_VERSION,
                    "width_source": norm.width_source,
                    "via_source": norm.via_source,
                    "grid_nm": req.grid_resolution,
                }
            ),
        )

    def _score(
        self, proposal: RouteProposal, validation: object, attempt: _Attempt, grid: SearchGrid
    ) -> RouteScore:
        bends = 0
        by_layer: dict[str, list[RouteSegment]] = {}
        for s in proposal.segments:
            by_layer.setdefault(s.layer, []).append(s)
        for a, b in zip(proposal.segments, proposal.segments[1:], strict=False):
            if (
                a.layer == b.layer
                and a.end == b.start
                and _direction(a.start, a.end) != _direction(b.start, b.end)
            ):
                bends += 1
        margin: float | None = None
        from pcbrouter.routing.validator import RouteValidationResult

        if isinstance(validation, RouteValidationResult):
            for el in validation.elements:
                r = el.result
                if r.minimum_observed_clearance is not None and r.required_clearance is not None:
                    m = r.minimum_observed_clearance - r.required_clearance
                    margin = m if margin is None else min(margin, m)
        exposure = 0.0
        cells = [(li, idx) for c in attempt.connections for li, idx in c.cells]
        if cells:
            vals = []
            for li, idx in cells:
                pen = grid.penalty[li]
                if pen is not None:
                    vals.append(float(pen.reshape(-1)[idx]))
            exposure = float(sum(vals) / len(cells)) if vals else 0.0
        return RouteScore(
            length_nm=proposal.length,
            vias=len(proposal.vias),
            bends=bends,
            layer_changes=len(proposal.vias),
            min_clearance_margin_nm=margin,
            congestion_exposure=min(1.0, exposure),
            cost=sum(c.cost for c in attempt.connections),
        )

    # ------------------------------------------------------------ failures
    @staticmethod
    def _search_failure(outcome: SearchOutcome, result: RouteResult) -> tuple[FailureReason, str]:
        if outcome.status is SearchStatus.CANCELLED:
            result.status = RouteStatus.CANCELLED
            return FailureReason.TIMEOUT, "cancelled by the user"
        if outcome.status in (SearchStatus.TIMEOUT, SearchStatus.NODE_LIMIT):
            result.status = RouteStatus.TIMEOUT
            what = "time limit" if outcome.status is SearchStatus.TIMEOUT else "node limit"
            return (
                FailureReason.TIMEOUT,
                f"search stopped at the {what} ({outcome.expanded:,} nodes)",
            )
        return FailureReason.NO_PATH, "no legal path on the routing grid"

    def _fail(
        self,
        result: RouteResult,
        attempt: _Attempt,
        norm: NormalisedRequest,
        grid: SearchGrid,
        groups: list[list[str]],
        cancel: threading.Event | None,
    ) -> None:
        result.reason = attempt.failure
        result.message = attempt.message
        if result.status not in (RouteStatus.CANCELLED, RouteStatus.TIMEOUT):
            result.status = RouteStatus.NO_ROUTE
        # blocking statistics (approximate: cell states in the searched window)
        stats: dict[str, int] = {}
        for layer in grid.layers:
            occ = self.engine.occupancy(
                layer, norm.net, norm.width, grid.spec.cell, grid.spec.bounds
            )
            for state, count in occ.counts().items():
                if state not in (CellState.FREE.label, CellState.SAME_NET.label):
                    stats[f"{layer} {state} cells"] = stats.get(f"{layer} {state} cells", 0) + count
        result.blockers = stats
        if attempt.failure is not FailureReason.NO_PATH:
            return
        # Would it route with more vias / more layers? (bounded diagnostic searches)
        req = norm.request
        if norm.max_vias is not None and norm.via_diameter is not None:
            relaxed = self._diagnose(replace(req, max_vias=None, candidates=1), cancel)
            if relaxed is not None and relaxed.best is not None:
                n = len(relaxed.best.proposal.vias)
                if n > norm.max_vias:
                    result.reason = FailureReason.VIA_LIMIT
                    result.message = (
                        f"a route exists with {n} via(s); the limit is " f"{norm.max_vias}"
                    )
                    return
        if req.allowed_layers is not None or req.forbidden_layers:
            relaxed = self._diagnose(
                replace(req, allowed_layers=None, forbidden_layers=(), candidates=1), cancel
            )
            if relaxed is not None and relaxed.best is not None:
                used = ", ".join(relaxed.best.proposal.layers)
                result.reason = FailureReason.LAYER_RESTRICTION
                result.message = f"a route exists using {used}, outside the allowed layers"

    def _diagnose(
        self, request: RouteRequest, cancel: threading.Event | None
    ) -> RouteResult | None:
        probe = replace(
            request,
            node_limit=min(request.node_limit, DIAGNOSE_NODE_LIMIT),
            time_limit_s=min(request.time_limit_s, 10.0),
        )
        sub = Router(self.engine, self.search_fn, self.backend_name)
        res = sub.route_net(probe, cancel=cancel)
        return res if res.success else None

    def _done(self, result: RouteResult, t0: float) -> RouteResult:
        result.metrics.elapsed_s = time.perf_counter() - t0
        log.info(
            "router.done net=%s status=%s reason=%s candidates=%d nodes=%d searches=%d "
            "repairs=%d ms=%.1f width=%s",
            result.net,
            result.status.value,
            result.reason.value if result.reason else "-",
            len(result.candidates),
            result.metrics.expanded_nodes,
            result.metrics.searches,
            result.metrics.repairs,
            result.metrics.elapsed_s * 1e3,
            format_mm(result.width) if result.width else "-",
        )
        return result
