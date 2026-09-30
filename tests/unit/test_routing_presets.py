"""R7 Speed/Accuracy presets: explicit tradeoffs, accuracy is today's behaviour."""

from __future__ import annotations

from pcbrouter.domain.units import mm_to_internal
from pcbrouter.routing.board_router import BoardRouterSettings
from pcbrouter.routing.presets import (
    SPEED_GRID_FLOOR_NM,
    RouteMode,
    adjust_board_settings,
    adjust_request,
)
from pcbrouter.routing.request import RouteRequest


def base_request() -> RouteRequest:
    return RouteRequest("N", grid_resolution=mm_to_internal(0.1))


def test_accuracy_keeps_current_behaviour() -> None:
    req = adjust_request(base_request(), RouteMode.ACCURACY)
    assert req.heuristic_weight == 1.0
    assert req.grid_resolution == mm_to_internal(0.1)
    assert req.candidates == 3
    settings = adjust_board_settings(
        BoardRouterSettings(max_passes=3, allow_ripup=True), base_request(), RouteMode.ACCURACY
    )
    assert settings.max_passes == 3 and settings.allow_ripup is True
    assert settings.optimize is False
    assert settings.base_request.heuristic_weight == 1.0


def test_speed_trades_quality_for_time() -> None:
    req = adjust_request(base_request(), RouteMode.SPEED)
    assert req.heuristic_weight == 1.5
    assert req.grid_resolution == SPEED_GRID_FLOOR_NM
    assert req.candidates == 1
    fine = RouteRequest("N", grid_resolution=mm_to_internal(0.5))
    assert adjust_request(fine, RouteMode.SPEED).grid_resolution == mm_to_internal(0.5)
    settings = adjust_board_settings(
        BoardRouterSettings(max_passes=3, allow_ripup=True), base_request(), RouteMode.SPEED
    )
    assert settings.max_passes == 2  # every net first (fair slice), then the unfinished
    assert settings.allow_ripup is False
    assert settings.optimize is False
    assert settings.base_request.heuristic_weight == 1.5
    assert settings.base_request.grid_resolution == SPEED_GRID_FLOOR_NM


def test_mode_labels() -> None:
    assert RouteMode("speed") is RouteMode.SPEED
    assert RouteMode("accuracy").label == "Accuracy"
