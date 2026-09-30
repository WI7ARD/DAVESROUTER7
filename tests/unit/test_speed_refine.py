"""Speed's targeted fine-grid retry (board_router.route_net_refined): a proven
NO_PATH/NO_ESCAPE on a grid coarser than 0.1 mm is retried once at 0.1 mm;
nothing else is retried, and Accuracy's default grid never is."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pcbrouter.routing.board_router import route_net_refined
from pcbrouter.routing.request import DEFAULT_GRID_NM, RouteRequest
from pcbrouter.routing.result import FailureReason, RouteMetrics, RouteResult, RouteStatus


@dataclass
class FakeRouter:
    fail_above: int
    reason: FailureReason = FailureReason.NO_PATH
    calls: list[int] = field(default_factory=list)

    def route_net(self, req: RouteRequest, **_kw: Any) -> RouteResult:
        self.calls.append(req.grid_resolution)
        ok = req.grid_resolution <= self.fail_above
        return RouteResult(
            req.request_id, req.net,
            RouteStatus.SUCCESS if ok else RouteStatus.NO_ROUTE,
            reason=None if ok else self.reason,
            metrics=RouteMetrics(expanded_nodes=100, grid_s=1.0),
        )  # fmt: skip


def test_coarse_no_path_retries_once_on_default_grid() -> None:
    router = FakeRouter(fail_above=DEFAULT_GRID_NM)
    res = route_net_refined(router, RouteRequest("N", grid_resolution=200_000))
    assert router.calls == [200_000, DEFAULT_GRID_NM]
    assert res.status is RouteStatus.SUCCESS
    assert res.metrics.expanded_nodes == 200 and res.metrics.grid_s == 2.0  # both counted


def test_default_grid_and_other_failures_are_not_retried() -> None:
    router = FakeRouter(fail_above=0)
    route_net_refined(router, RouteRequest("N", grid_resolution=DEFAULT_GRID_NM))
    assert router.calls == [DEFAULT_GRID_NM]  # Accuracy: unchanged behaviour
    timeout = FakeRouter(fail_above=0, reason=FailureReason.TIMEOUT)
    route_net_refined(timeout, RouteRequest("N", grid_resolution=200_000))
    assert timeout.calls == [200_000]  # a limit, not a grid problem
    still = FakeRouter(fail_above=0)
    res = route_net_refined(still, RouteRequest("N", grid_resolution=200_000))
    assert still.calls == [200_000, DEFAULT_GRID_NM] and res.status is RouteStatus.NO_ROUTE
