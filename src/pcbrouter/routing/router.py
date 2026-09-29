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
from typing import Any

import numpy as np
import numpy.typing as npt

from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.geometry import BoundingBox, Point
from pcbrouter.domain.units import Nm, format_mm
from pcbrouter.geometry.board import ItemKind
from pcbrouter.routing.collision import CollisionResult, ValidationStatus
from pcbrouter.routing.connectivity import net_connectivity
from pcbrouter.routing.occupancy import GridSpec
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
from pcbrouter.routing.search.grid import GridCancelled, SearchGrid, compile_grid

log = logging.getLogger(__name__)

ROUTER_VERSION = "1.0.0"
MAX_REPAIRS = 6
#: one-shot weight when a search hits its time/node limit (R6-power): proven
#: ≤1.5x optimal bound, still exact-validated downstream.
ESCALATION_WEIGHT = 1.5
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
    where: dict[str, Any] | None = None


#: source-cell budget per search (R6-power): pour interiors are redundant —
#: any path reaching the copper connects — so deep-interior cells are stride
#: sampled while boundary cells are always kept. Extra (newly routed) sources
#: merge after thinning and are never thinned.
SOURCE_CELL_CAP = 65536


def _thin_sources(
    sources: list[npt.NDArray[np.int64]], nx: int, ny: int, cap: int = SOURCE_CELL_CAP
) -> list[npt.NDArray[np.int64]]:
    """Boundary-preserving source thinning (deterministic)."""
    total = sum(len(s) for s in sources)
    if total <= cap:
        return sources
    stride = max(2, total // cap)
    out: list[npt.NDArray[np.int64]] = []
    for cells in sources:
        if len(cells) == 0:
            out.append(cells)
            continue
        mask = np.zeros(nx * ny, dtype=np.bool_)
        mask[cells] = True
        view = mask.reshape(ny, nx)
        pad = np.zeros((ny + 2, nx + 2), dtype=np.bool_)
        pad[1:-1, 1:-1] = view
        interior = (
            view
            & pad[:-2, 1:-1]
            & pad[2:, 1:-1]
            & pad[1:-1, :-2]
            & pad[1:-1, 2:]
            & pad[:-2, :-2]
            & pad[:-2, 2:]
            & pad[2:, :-2]
            & pad[2:, 2:]
        )
        is_inside = interior.reshape(-1)[cells]
        kept = np.unique(np.concatenate([cells[~is_inside], cells[is_inside][::stride]]))
        out.append(kept)
    return out


class Router:
    def __init__(
        self,
        engine: BoardEngine,
        search_fn: SearchFn | None = None,
        backend_name: str = "cpu",
        grid_cache: dict[Any, Any] | None = None,
    ) -> None:
        self.engine = engine
        self.search_fn: SearchFn = search_fn or search
        self.backend_name = backend_name
        #: shared compiled-grid inputs across passes/rip-ups (see grid.compile_grid)
        self.grid_cache = grid_cache
        #: record the cells a failed search explored (debug overlay / playback)
        self.record_explored = False
        #: optional progress sink (phase/candidate/connection dicts); called a few
        #: times per net, never per search node. Must be cheap and thread-safe.
        self.progress: Callable[[dict[str, Any]], None] | None = None
        self._deadline: float | None = None

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
        self._deadline = (
            None if request.total_time_limit_s is None else t0 + request.total_time_limit_s
        )
        result = RouteResult(request.request_id, request.net, RouteStatus.NO_ROUTE)
        result.metrics.backend = self.backend_name
        req = request
        for round_no in range(3):  # initial attempt + up to two limit doublings
            try:
                norm = normalise(self.engine, req)
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
            except GridCancelled:
                result.status, result.reason = RouteStatus.CANCELLED, FailureReason.TIMEOUT
                result.message = "cancelled by the user"
                return self._done(result, t0)
            except Exception as exc:  # reported, never hidden; the board is untouched
                log.exception("router.internal_error net=%s", req.net)
                result.status, result.message = RouteStatus.INTERNAL_ERROR, repr(exc)
                result.candidates = []
                return self._done(result, t0)
            if not self._limits_escalate(req, result, round_no, cancel):
                break
            req = replace(
                req,
                node_limit=req.node_limit * 2,
                time_limit_s=req.time_limit_s * 1.5,
            )
            result.status, result.reason = RouteStatus.NO_ROUTE, None
            result.details.append(
                "search hit the request limits; retrying with doubled budget "
                f"(round {round_no + 2} of 3)"
            )
        return self._done(result, t0)

    def _limits_escalate(
        self,
        req: RouteRequest,
        result: RouteResult,
        round_no: int,
        cancel: threading.Event | None,
    ) -> bool:
        """Whether a TIMEOUT deserves another round with doubled search limits.

        Bounded (two extra rounds) and skipped where something else owns the
        budget: board jobs carry ``total_time_limit_s`` (their passes escalate
        themselves), Speed mode stays single-attempt, and a set cancel or a
        blown deadline stops immediately.
        """
        blown = self._deadline is not None and time.perf_counter() > self._deadline
        cancelled = cancel is not None and cancel.is_set()
        return (
            result.status is RouteStatus.TIMEOUT
            and result.reason is FailureReason.TIMEOUT
            and round_no < 2
            and req.heuristic_weight < ESCALATION_WEIGHT
            and req.total_time_limit_s is None
            and not cancelled
            and not blown
        )

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
        groups, dropped, missed = self._order_groups(groups, norm.request)
        if missed:
            result.status = RouteStatus.INVALID_REQUEST
            result.message = "source group matches no copper on this net"
            return
        if dropped:
            result.details.append(
                f"source/target selection leaves {dropped} copper group(s) "
                "unrouted (not part of this request)"
            )
        result.connections_total = len(groups) - 1
        windows: list[BoundingBox | None] = [self._window(groups)]
        if windows[0] is not None:
            windows.append(None)
        for window in windows:
            if cancel is not None and cancel.is_set():
                result.status, result.reason = RouteStatus.CANCELLED, FailureReason.TIMEOUT
                result.message = "cancelled by the user"
                return
            if self._deadline is not None and time.perf_counter() > self._deadline:
                # budget spent: fail fast without burning a grid build + search
                # that could only time out (stays TIMEOUT so passes escalate it)
                result.status, result.reason = RouteStatus.TIMEOUT, FailureReason.TIMEOUT
                result.message = "net time budget spent"
                return
            self._report(
                phase="BUILDING_GRID",
                net=norm.net,
                window="net neighbourhood" if window is not None else "whole board",
            )
            grid = None
            try:
                t_grid = time.perf_counter()
                window_key = (
                    None
                    if window is None
                    else (window.min_x, window.min_y, window.max_x, window.max_y)
                )
                via = norm.via_diameter if norm.vias_allowed else None
                grid_key = (
                    (
                        self.engine.cache_key,
                        norm.net,
                        norm.width,
                        via,
                        norm.layers,
                        norm.request.grid_resolution,
                        window_key,
                    )
                    if self.grid_cache is not None
                    else None
                )
                grid = compile_grid(
                    self.engine,
                    norm.net,
                    norm.layers,
                    norm.width,
                    via,
                    norm.request.grid_resolution,
                    window,
                    progress=self._grid_progress(norm.net),
                    cancel=cancel,
                    cache=self.grid_cache,
                    cache_key=grid_key,
                )
                result.metrics.grid_s += time.perf_counter() - t_grid
            except GridCancelled:
                result.status, result.reason = RouteStatus.CANCELLED, FailureReason.TIMEOUT
                result.message = "cancelled by the user"
                return
            self._apply_costs(grid, norm, penalties, avoid_uids)
            result.metrics.grid_cells = grid.n * len(grid.layers)
            self._report(
                phase="ROUTING",
                net=norm.net,
                grid=(grid.nx, grid.ny, len(grid.layers)),
                connections_total=len(groups) - 1,
            )
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
        partial: _Attempt | None = None
        for k in range(norm.request.candidates):
            self._report(
                phase="ROUTING",
                net=norm.net,
                candidates_completed=k,
                candidates_total=norm.request.candidates,
            )
            attempt = self._attempt(grid, norm, groups, result, cancel)
            if attempt.failure is not None and not attempt.connections:
                if k == 0:
                    self._fail(result, attempt, norm, grid, groups, cancel)
                    result.failed_at = attempt.where
                break
            if attempt.failure is not None and partial is None:
                partial = attempt
            proposal = self._proposal(norm, attempt, k)
            key = (proposal.segments, proposal.vias)
            if key in seen:
                self._penalise(grid, attempt, norm)
                continue
            seen.add(key)
            self._report(phase="VALIDATING", net=norm.net)
            t_validate = time.perf_counter()
            validation = self.engine.validator.validate_route(proposal)
            result.metrics.validate_s += time.perf_counter() - t_validate
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
        if full:
            # success clears any stale failure text from an earlier round
            result.reason, result.message = None, ""
        elif result.reason is None:
            # keep the real reason (node/time limit, no path, ...) and say how far
            result.reason = partial.failure if partial is not None else FailureReason.NO_PATH
            result.message = (
                f"{best.connections}/{result.connections_total} connection(s) routed; "
                + (partial.message if partial is not None else "the rest found no path")
            )
            result.failed_at = partial.where if partial is not None else None
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
        # Group cells are computed ONCE (not per connection): the old code
        # re-rasterised every group on every connection (O(n^2) in groups)
        # with no cancel point, stalling silently on pour-heavy nets.
        group_cells_all: list[list[npt.NDArray[np.int64]]] = []
        for j, g in enumerate(groups):
            if cancel is not None and cancel.is_set():
                raise GridCancelled()
            self._report(
                phase="ROUTING",
                net=norm.net,
                message=f"locating copper (group {j + 1}/{len(groups)})",
            )
            group_cells_all.append(self._cells_of(grid, g, cancel))
        # Power nets: start from the smallest copper group. Pour-heavy groups
        # hold millions of cells; pushing all of them as sources explodes the
        # heap before the first expansion. Connectivity is symmetric, so the
        # start side only affects speed, never correctness.
        sizes = [sum(c.size for c in gc) for gc in group_cells_all]
        start = min(range(len(groups)), key=lambda i: (sizes[i], i))
        connected_idx = {start}
        remaining_idx = [j for j in range(len(groups)) if j != start]
        # cell -> group index per layer, for attributing reached endpoints
        cell_to_group: list[dict[int, int]] = [{} for _ in range(nl)]
        for j, gc in enumerate(group_cells_all):
            for li in range(nl):
                for idx in gc[li].tolist():
                    cell_to_group[li].setdefault(int(idx), j)
        extra_sources: list[list[int]] = [[] for _ in range(nl)]
        vias_used = 0
        while remaining_idx:
            if cancel is not None and cancel.is_set():
                raise GridCancelled()
            merged = [
                np.unique(np.concatenate([group_cells_all[j][li] for j in sorted(connected_idx)]))
                for li in range(nl)
            ]
            sources = _thin_sources(merged, grid.nx, grid.ny)
            for li in range(nl):
                if extra_sources[li]:
                    sources[li] = np.unique(
                        np.concatenate([sources[li], np.asarray(extra_sources[li])])
                    )
            target_masks = [np.zeros((grid.ny, grid.nx), dtype=np.bool_) for _ in range(nl)]
            for j in remaining_idx:
                for li in range(nl):
                    target_masks[li].reshape(-1)[group_cells_all[j][li]] = True
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
            boxes: list[tuple[int, int, int, int, int]] = []
            for j in remaining_idx:  # one heuristic box per unconnected group
                for li in range(nl):
                    cells = group_cells_all[j][li]
                    if cells.size:
                        rows, cols = np.divmod(cells, grid.nx)
                        boxes.append((li, int(rows.min()), int(rows.max()),
                                      int(cols.min()), int(cols.max())))  # fmt: skip
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
                target_boxes=tuple(boxes),
                layer_dirs=self._layer_dirs(norm, grid),
                coarse_factor=norm.request.coarse_factor,
            )
            connection = None
            self._report(
                phase="ROUTING",
                net=norm.net,
                connection=len(attempt.connections) + 1,
                connections_total=attempt.total,
                message="searching",
            )
            for _repair in range(MAX_REPAIRS + 1):
                t_search = time.perf_counter()
                outcome = self.search_fn(
                    problem,
                    node_limit=norm.request.node_limit,
                    time_limit_s=self._search_time(norm.request.time_limit_s),
                    cancel=cancel,
                    record_explored=self.record_explored,
                    heuristic_weight=norm.request.heuristic_weight,
                )
                result.metrics.search_s += time.perf_counter() - t_search
                result.metrics.expanded_nodes += outcome.expanded
                result.metrics.searches += 1
                if (
                    outcome.status
                    in (
                        SearchStatus.TIMEOUT,
                        SearchStatus.NODE_LIMIT,
                    )
                    and norm.request.heuristic_weight < ESCALATION_WEIGHT
                ):
                    # Limits bound, not space: one weighted retry (proven ≤1.5x
                    # optimal bound, validator still gates) in remaining budget.
                    # Skipped when the user already asked for weighted search.
                    if cancel is not None and cancel.is_set():
                        outcome = SearchOutcome(SearchStatus.CANCELLED)
                    else:
                        limit_name = (
                            "time limit" if outcome.status is SearchStatus.TIMEOUT else "node limit"
                        )
                        t_esc = time.perf_counter()
                        outcome = self.search_fn(
                            problem,
                            node_limit=norm.request.node_limit,
                            time_limit_s=self._search_time(norm.request.time_limit_s),
                            cancel=cancel,
                            record_explored=self.record_explored,
                            heuristic_weight=ESCALATION_WEIGHT,
                        )
                        result.metrics.search_s += time.perf_counter() - t_esc
                        result.metrics.expanded_nodes += outcome.expanded
                        result.metrics.searches += 1
                        result.details.append(
                            f"search hit the {limit_name}; retried weighted "
                            f"(≤{ESCALATION_WEIGHT}x optimal bound)"
                        )
                if outcome.status is not SearchStatus.FOUND:
                    attempt.failure, attempt.message = self._search_failure(outcome, result)
                    attempt.where = self._where(
                        grid,
                        [groups[j] for j in sorted(connected_idx)],
                        [groups[j] for j in remaining_idx],
                    )
                    if outcome.explored is not None:
                        result.explored = {"spec": grid.spec, "cells": outcome.explored,
                                           "layers": grid.layers}  # fmt: skip
                    return attempt
                connection, bad = self._geometry_timed(grid, norm, outcome, result)
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
            self._report(
                phase="ROUTING",
                net=norm.net,
                connection=len(attempt.connections),
                connections_total=attempt.total,
            )
            end_li, end_idx = connection.cells[-1]
            hit = cell_to_group[end_li].get(int(end_idx))
            if hit is None or hit not in remaining_idx:
                # Shared cells map first-wins above; attribute exactly instead
                # of guessing remaining_idx[0] (a wrong guess ends the loop
                # early and reports SUCCESS with a group still isolated).
                hit = next(
                    (
                        j
                        for j in remaining_idx
                        if np.any(group_cells_all[j][end_li] == int(end_idx))
                    ),
                    None,
                )
            if hit is None:
                attempt.failure = FailureReason.VALIDATION
                attempt.message = "routed endpoint touches no remaining copper group"
                return attempt
            remaining_idx.remove(hit)
            connected_idx.add(hit)
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
    def _geometry_timed(
        self,
        grid: SearchGrid,
        norm: NormalisedRequest,
        outcome: SearchOutcome,
        result: RouteResult,
    ) -> tuple[_Connection, list[tuple[int, npt.NDArray[np.int64]]]]:
        t0 = time.perf_counter()
        try:
            return self._geometry(grid, norm, outcome)
        finally:
            result.metrics.geometry_s += time.perf_counter() - t0

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
    def _where(
        self, grid: SearchGrid, connected: list[list[str]], remaining: list[list[str]]
    ) -> dict[str, Any]:
        """Where a connection search failed: the connected copper closest to the
        nearest unconnected pad group (millimetres), and the layers searched."""
        geo = self.engine.geometry

        def centres(uids: list[str]) -> list[Point]:
            return [geo.copper[u].bounds.center for u in uids if u in geo.copper]

        src = centres([u for g in connected for u in g])[:200]
        dst = centres([u for g in remaining for u in g])[:200]
        out: dict[str, Any] = {"layers": list(grid.layers), "remaining_groups": len(remaining)}
        if src and dst:
            a, b = min(
                ((p, q) for p in src for q in dst),
                key=lambda pq: (pq[0].x - pq[1].x) ** 2 + (pq[0].y - pq[1].y) ** 2,
            )
            out["start"] = (round(a.x / 1e6, 3), round(a.y / 1e6, 3))
            out["goal"] = (round(b.x / 1e6, 3), round(b.y / 1e6, 3))
        return out

    def _cells_of(
        self,
        grid: SearchGrid,
        uids: list[str],
        cancel: threading.Event | None = None,
    ) -> list[npt.NDArray[np.int64]]:
        geo = self.engine.geometry
        per_layer: list[list[npt.NDArray[np.int64]]] = [[] for _ in grid.layers]
        for n, uid in enumerate(uids):
            # Cancel point for pour-heavy groups (thousands of copper objects).
            if cancel is not None and n % 256 == 0 and cancel.is_set():
                raise GridCancelled()
            item = geo.copper.get(uid)
            if item is None:
                continue
            for li, layer in enumerate(grid.layers):
                if layer not in item.layers:
                    continue
                for s in item.shapes:
                    cells = grid.cells_within(s, 0.0, cancel)
                    if cells.size == 0:
                        idx = grid.index_of(item.bounds.center)
                        if idx is not None:
                            cells = np.asarray([idx], dtype=np.int64)
                    per_layer[li].append(cells)
        return [
            np.unique(np.concatenate(c)) if c else np.zeros(0, dtype=np.int64) for c in per_layer
        ]

    @staticmethod
    def _order_groups(
        groups: list[list[str]], request: RouteRequest
    ) -> tuple[list[list[str]], int, bool]:
        """Order groups for source-first routing.

        Returns (ordered, dropped, source_missed): ``dropped`` counts copper
        groups a source/target selection leaves out (they stay unrouted — the
        caller must say so instead of reporting full success);
        ``source_missed`` means the requested source matches nothing.
        """
        if request.source_group:
            src = set(request.source_group)
            first = [g for g in groups if src & set(g)]
            if first:
                rest = [g for g in groups if g is not first[0]]
                if request.target_group:
                    tgt = set(request.target_group)
                    kept = [g for g in rest if tgt & set(g)]
                    return [first[0], *kept], len(rest) - len(kept), False
                return [first[0], *rest], 0, False
            return (
                sorted(groups, key=lambda g: (-len(g), g)),
                0,
                True,
            )
        # deterministic: largest group first (most pads), ties by uid order
        return sorted(groups, key=lambda g: (-len(g), g)), 0, False

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
    def _layer_dirs(norm: NormalisedRequest, grid: SearchGrid) -> tuple[int, ...]:
        """Alternating preferred directions (H, V, H, ...) in stack order when the
        cost model asks for them; () = no direction preference."""
        if norm.request.cost.wrong_way_factor <= 1.0 or len(grid.layers) < 2:
            return ()
        return tuple(1 if i % 2 == 0 else 2 for i in range(len(grid.layers)))

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
        from pcbrouter.geometry.shapes import rectangle as _rect

        for box in norm.request.blocked_regions:  # user region locks: hard for new copper
            cells = grid.cells_within(_rect(box.center, box.width, box.height), norm.width / 2)
            for li in range(len(grid.layers)):
                grid.block(li, cells)
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
        # blocking statistics from the searched grid itself: repairs already
        # mutated passable, so this reflects what the search saw — without a
        # full occupancy rebuild per failed net.
        stats: dict[str, int] = {}
        for layer, cells in zip(grid.layers, grid.passable, strict=True):
            blocked = int((~cells.reshape(-1)).sum())
            if blocked:
                stats[f"{layer} blocked cells"] = blocked
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

    def _search_time(self, per_search: float) -> float:
        """Per-search limit, capped by what is left of the net's total budget."""
        if self._deadline is None:
            return per_search
        return max(0.001, min(per_search, self._deadline - time.perf_counter()))

    def _grid_progress(self, net: str) -> Callable[[str], None]:
        def tell(message: str) -> None:
            self._report(phase="BUILDING_GRID", net=net, message=message)

        return tell

    def _report(self, **info: Any) -> None:
        if self.progress is not None:
            try:
                self.progress(info)
            except Exception:
                log.exception("router progress callback failed")

    def _diagnose(
        self, request: RouteRequest, cancel: threading.Event | None
    ) -> RouteResult | None:
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            return None  # the net's budget is spent: skip optional diagnostics
        probe = replace(
            request,
            node_limit=min(request.node_limit, DIAGNOSE_NODE_LIMIT),
            time_limit_s=min(request.time_limit_s, 10.0),
            total_time_limit_s=(
                None if self._deadline is None else max(0.001, self._deadline - time.perf_counter())
            ),
        )
        sub = Router(self.engine, self.search_fn, self.backend_name)
        res = sub.route_net(probe, cancel=cancel)
        return res if res.success else None

    def _done(self, result: RouteResult, t0: float) -> RouteResult:
        result.metrics.elapsed_s = time.perf_counter() - t0
        used = getattr(self.search_fn, "used", None)
        if isinstance(used, dict):  # hybrid backend: report what actually ran
            result.metrics.backend = (
                f"{self.backend_name} (cpu {used.get('cpu', 0)}, gpu {used.get('gpu', 0)}, "
                f"fallback {used.get('fallback', 0)})"
            )
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
        if result.status not in (RouteStatus.SUCCESS, RouteStatus.ALREADY_CONNECTED):
            log.info("[SEARCH] %s", result.failure_report().replace("\n", " "))
        return result
