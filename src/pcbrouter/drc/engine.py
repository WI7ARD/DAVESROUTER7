"""Internal Geometry Check engine (NOT KiCad DRC; see docs/internal_drc.md)."""

from __future__ import annotations

import logging
import time

from pcbrouter.drc.checks import ALL_CHECKS, CheckContext
from pcbrouter.drc.result import CHECK_NAME, DRCResult
from pcbrouter.geometry.board import BoardGeometry
from pcbrouter.routing.collision import CollisionEngine
from pcbrouter.routing.connectivity import BoardConnectivity
from pcbrouter.rules.resolver import RuleResolver

log = logging.getLogger(__name__)


class DRCError(Exception):
    """The internal geometry check could not run."""


def run_geometry_check(
    geometry: BoardGeometry,
    resolver: RuleResolver,
    connectivity: BoardConnectivity | None = None,
) -> DRCResult:
    """Run every supported check over the board's existing copper. Deterministic:
    violations are sorted by (severity, kind, id)."""
    t0 = time.perf_counter()
    margin = CollisionEngine(geometry, resolver).search_margin
    ctx = CheckContext(geometry, resolver, connectivity, margin)
    for name, check in ALL_CHECKS:
        t = time.perf_counter()
        check(ctx)
        log.debug("drc.check name=%s ms=%.1f", name, (time.perf_counter() - t) * 1e3)
    ctx.violations.sort(key=lambda v: (v.severity.rank, v.kind.value, v.id))
    result = DRCResult(
        violations=ctx.violations,
        check_count=ctx.checks,
        elapsed_time=time.perf_counter() - t0,
        board_fingerprint=geometry.fingerprint,
        rules_digest=resolver.ruleset.digest,
        skipped=dict(sorted(ctx.skipped.items())),
    )
    log.info(
        "drc.done name=%r status=%s errors=%d warnings=%d info=%d checks=%d ms=%.1f skipped=%s",
        CHECK_NAME, result.status.value, len(result.errors), len(result.warnings),
        len(result.infos), result.check_count, result.elapsed_time * 1e3, result.skipped,
    )  # fmt: skip
    return result
