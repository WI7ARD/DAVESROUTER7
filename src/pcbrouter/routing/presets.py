"""Speed / Accuracy routing presets (R7).

Two named operating points (tuned on Router_Benchmark_RevA, a 2-layer board
built to force many crossings: both route 32/32 nets with 0 DRC errors):

* Accuracy: full-resolution grid, admissible search (weight 1.0), configured
  candidates/passes/rip-up, preferred layer directions (wrong-way factor 3) and
  coarse-to-fine search (x4). Each search is optimal inside a corridor around a
  coarse route, with a full search as fallback, so no route is lost.
* Speed: coarser grid floor (200 µm), weighted search (1.5, proven bound:
  cost at most 1.5x optimal), single candidates, two passes (every net first,
  then the unfinished ones), no rip-up, stronger
  layer directions (4) and coarse-to-fine (x4).

Layer directions alternate H/V in stack order (first copper layer horizontal);
they only apply on boards with two or more routing layers.

Presets only instantiate explicit user choice (the Route panel toggle): the
router never silently degrades quality.
"""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum

from pcbrouter.routing.board_router import BoardRouterSettings
from pcbrouter.routing.request import RouteRequest

SPEED_GRID_FLOOR_NM = 200_000
SPEED_HEURISTIC_WEIGHT = 1.5
ACCURACY_WRONG_WAY = 3.0
SPEED_WRONG_WAY = 4.0
COARSE_FACTOR = 4


class RouteMode(StrEnum):
    ACCURACY = "accuracy"
    SPEED = "speed"

    @property
    def label(self) -> str:
        return self.value.capitalize()


def adjust_request(request: RouteRequest, mode: RouteMode) -> RouteRequest:
    """Apply the mode to a single-net RouteRequest (returns a new request)."""
    if mode is not RouteMode.SPEED:
        return replace(
            request,
            heuristic_weight=1.0,
            coarse_factor=COARSE_FACTOR,
            cost=replace(request.cost, wrong_way_factor=ACCURACY_WRONG_WAY),
        )
    return replace(
        request,
        heuristic_weight=SPEED_HEURISTIC_WEIGHT,
        grid_resolution=max(request.grid_resolution, SPEED_GRID_FLOOR_NM),
        candidates=1,
        coarse_factor=COARSE_FACTOR,
        cost=replace(request.cost, wrong_way_factor=SPEED_WRONG_WAY),
    )


def adjust_board_settings(
    settings: BoardRouterSettings, base_request: RouteRequest, mode: RouteMode
) -> BoardRouterSettings:
    """Apply the mode to BoardRouterSettings under construction."""
    if mode is not RouteMode.SPEED:
        return replace(settings, base_request=adjust_request(base_request, mode))
    return replace(
        settings,
        max_passes=2,  # pass 1 gives every net a fair time slice, pass 2 finishes
        allow_ripup=False,
        optimize=False,
        base_request=adjust_request(base_request, mode),
    )
