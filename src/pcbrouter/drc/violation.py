"""Internal geometry-check violation model (not KiCad DRC — see docs/internal_drc.md)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from pcbrouter.domain.geometry import Point
from pcbrouter.geometry.shapes import ShapeAccuracy


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"error": 0, "warning": 1, "info": 2}[self.value]


class ViolationKind(Enum):
    SHORT = "short circuit"
    TRACK_TRACK_CLEARANCE = "trace-to-trace clearance"
    TRACK_PAD_CLEARANCE = "trace-to-pad clearance"
    TRACK_VIA_CLEARANCE = "trace-to-via clearance"
    VIA_COPPER_CLEARANCE = "via-to-copper clearance"
    PAD_PAD_CLEARANCE = "pad-to-pad clearance"
    ZONE_CLEARANCE = "zone-to-copper clearance"
    MIN_TRACK_WIDTH = "minimum track width"
    MAX_TRACK_WIDTH = "maximum track width"
    MIN_VIA_DIAMETER = "minimum via diameter"
    MIN_DRILL = "minimum drill"
    MIN_ANNULAR = "minimum annular ring"
    EDGE_CROSSING = "copper crossing board edge"
    EDGE_CLEARANCE = "board-edge clearance"
    OUTSIDE_BOARD = "copper outside board"
    TRACK_IN_KEEPOUT = "track inside keepout"
    VIA_IN_KEEPOUT = "via inside keepout"
    PAD_IN_KEEPOUT = "pad inside keepout"
    HOLE_CLEARANCE = "copper-to-hole clearance"
    HOLE_TO_HOLE = "hole-to-hole clearance"
    DISALLOWED = "disallowed by custom rule"
    UNCONNECTED = "unconnected items"
    RULE_UNKNOWN = "rule unknown"
    UNSUPPORTED_RULE = "unsupported rule"
    GEOMETRY_NOTE = "geometry note"


@dataclass(frozen=True, slots=True)
class DRCViolation:
    id: str
    kind: ViolationKind
    severity: Severity
    message: str
    object_a: str | None = None  # geometry uid, e.g. "track:<uuid>"
    object_b: str | None = None
    layer: str | None = None
    location: Point | None = None
    actual_value: float | None = None  # nm
    required_value: int | None = None  # nm
    rule_source: str | None = None
    net_a: str | None = None
    net_b: str | None = None
    accuracy: ShapeAccuracy = ShapeAccuracy.EXACT

    @property
    def nets(self) -> str:
        return " / ".join(n for n in (self.net_a, self.net_b) if n) or "—"


def violation_id(kind: ViolationKind, *parts: object) -> str:
    """Stable id: same board + same finding -> same id (for tests and UI state)."""
    digest = hashlib.sha1(repr((kind.value, *parts)).encode(), usedforsecurity=False)
    return f"drc-{digest.hexdigest()[:12]}"
