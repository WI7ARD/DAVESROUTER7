"""Speed / Accuracy routing presets (R7).

Two named operating points; Accuracy is current behaviour exactly:

* Accuracy: full-resolution grid, admissible search (weight 1.0), configured
  candidates/passes/rip-up.
* Speed: coarser grid floor (200 µm), weighted search (1.5, proven bound:
  cost at most 1.5x optimal), single candidates, one pass, no rip-up.

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


class RouteMode(StrEnum):
    ACCURACY = "accuracy"
    SPEED = "speed"

    @property
    def label(self) -> str:
        return self.value.capitalize()


def adjust_request(request: RouteRequest, mode: RouteMode) -> RouteRequest:
    """Apply the mode to a single-net RouteRequest (returns a new request)."""
    if mode is not RouteMode.SPEED:
        return replace(request, heuristic_weight=1.0)
    return replace(
        request,
        heuristic_weight=SPEED_HEURISTIC_WEIGHT,
        grid_resolution=max(request.grid_resolution, SPEED_GRID_FLOOR_NM),
        candidates=1,
    )


def adjust_board_settings(
    settings: BoardRouterSettings, base_request: RouteRequest, mode: RouteMode
) -> BoardRouterSettings:
    """Apply the mode to BoardRouterSettings under construction."""
    if mode is not RouteMode.SPEED:
        return replace(settings, base_request=replace(base_request, heuristic_weight=1.0))
    return replace(
        settings,
        max_passes=1,
        allow_ripup=False,
        optimize=False,
        base_request=replace(
            base_request,
            heuristic_weight=SPEED_HEURISTIC_WEIGHT,
            grid_resolution=max(base_request.grid_resolution, SPEED_GRID_FLOOR_NM),
            candidates=1,
        ),
    )
