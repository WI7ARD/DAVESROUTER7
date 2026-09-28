"""Manual trace/via drawing: pure geometry helpers (no Qt).

The UI controller (:mod:`pcbrouter.ui.manual_draw`) collects vertices on the
canvas and commits them through :class:`CommitManualCopperCommand`; everything
here is unit-testable without a display:

* grid + 45-degree snapping,
* width/via size resolution with the same fail-fast RULE_UNKNOWN policy as the
  router (a Workbench width alone is not verifiable without a rule minimum),
* building validated :class:`Track` / :class:`Via` objects with unique ids.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from itertools import pairwise

from pcbrouter.domain.geometry import Point
from pcbrouter.domain.track import Track
from pcbrouter.domain.units import Nm, internal_to_mm, mm_to_internal
from pcbrouter.domain.via import Via, ViaType
from pcbrouter.routing.working_board import WorkingBoard, stable_id

#: Default snap grid when the canvas grid is hidden (50 um).
DEFAULT_SNAP_MM = 0.05


class ManualDrawError(RuntimeError):
    """Manual drawing was refused before touching the board."""


@dataclass(frozen=True, slots=True)
class ManualViaSizes:
    """Resolved via size for one hand-placed via on ``net``."""

    diameter: Nm
    drill: Nm


def snap_to_grid(point: Point, grid_nm: int) -> Point:
    """Snap ``point`` to the nearest multiple of ``grid_nm`` (no-op if <= 0)."""
    if grid_nm <= 0:
        return point
    return Point(
        round(point.x / grid_nm) * grid_nm,
        round(point.y / grid_nm) * grid_nm,
    )


def snap_45(start: Point, end: Point) -> Point:
    """Snap the ``start`` -> ``end`` direction to the nearest octilinear angle.

    Length is preserved; a zero-length vector is returned unchanged.
    """
    dx, dy = end.x - start.x, end.y - start.y
    if dx == 0 and dy == 0:
        return end
    length = math.hypot(dx, dy)
    snapped = round(math.degrees(math.atan2(dy, dx)) / 45.0) * 45.0
    rad = math.radians(snapped)
    return Point(
        start.x + round(length * math.cos(rad)),
        start.y + round(length * math.sin(rad)),
    )


def resolve_manual_width(wb: WorkingBoard, net: str) -> Nm:
    """Resolve the trace width for a hand-drawn trace on ``net``.

    Precedence: Workbench width constraint, then the rule preferred width,
    then the rule minimum. Raises :class:`ManualDrawError` when no width is
    known (same no-guessing policy as the router) or when the user's width
    is below the hard minimum.
    """
    constraints = wb.net_constraints.get(net, {})
    width_mm = constraints.get("width_mm")
    width_rules = wb.engine.resolver.width_rules(net)
    minimum = width_rules.minimum.value
    width: Nm | None = None
    if isinstance(width_mm, (int, float)):
        width = mm_to_internal(float(width_mm))
        if minimum is not None and width < minimum:
            raise ManualDrawError(
                f"width {internal_to_mm(width):g} mm is below the hard minimum "
                f"{internal_to_mm(minimum):g} mm for {net}"
            )
    else:
        preferred = width_rules.preferred.value
        width = preferred if preferred is not None else minimum
    if width is None:
        raise ManualDrawError(
            f"no rule states a minimum track width for {net} (add net classes in "
            "KiCad so minimums are known, or turn conservative rule handling off "
            "in Settings as an explicit expert choice; a Workbench width alone "
            "is not verifiable without a minimum)"
        )
    return width


def resolve_manual_via(wb: WorkingBoard, net: str) -> ManualViaSizes:
    """Resolve the via size for a hand-placed via on ``net``.

    Like the router (unknown via size means vias unavailable), this refuses
    instead of guessing a default size.
    """
    via_rules = wb.engine.resolver.resolve_via_rules(net)
    diameter = via_rules.diameter.value
    if diameter is None:
        diameter = via_rules.min_diameter.value
    drill = via_rules.drill.value
    if drill is None:
        drill = via_rules.min_drill.value
    if diameter is None or drill is None:
        raise ManualDrawError(
            f"no rule states a via size for {net} (add net classes in KiCad "
            "so via diameter/drill are known, or turn conservative rule "
            "handling off in Settings as an explicit expert choice)"
        )
    return ManualViaSizes(diameter=diameter, drill=drill)


def new_run_id() -> str:
    """Unique id prefix so two identical hand-drawn shapes never share ids."""
    return uuid.uuid4().hex[:12]


def build_manual_tracks(
    net: str, points: list[Point], width: Nm, layer: str, run_id: str
) -> list[Track]:
    """One :class:`Track` per consecutive point pair; zero-length pairs skipped."""
    tracks: list[Track] = []
    for i, (start, end) in enumerate(pairwise(points)):
        if start == end:
            continue
        tracks.append(
            Track(
                stable_id("manual", run_id, net, layer, i, start.x, start.y, end.x, end.y),
                start,
                end,
                width,
                layer,
                net,
            )
        )
    return tracks


def build_manual_via(
    net: str,
    position: Point,
    diameter: Nm,
    drill: Nm,
    start_layer: str,
    end_layer: str,
    run_id: str,
) -> Via:
    """A through via spanning ``start_layer`` -> ``end_layer``."""
    return Via(
        stable_id("manual", run_id, net, "via", position.x, position.y),
        position,
        diameter,
        drill,
        net,
        start_layer,
        end_layer,
        ViaType.THROUGH,
    )
