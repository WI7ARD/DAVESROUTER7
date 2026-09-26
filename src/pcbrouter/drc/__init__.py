"""Internal Geometry Check (Stage 3). Not KiCad DRC — see docs/internal_drc.md."""

from __future__ import annotations

from pcbrouter.drc.engine import DRCError, run_geometry_check
from pcbrouter.drc.result import CHECK_NAME, DRCResult, DRCStatus
from pcbrouter.drc.violation import DRCViolation, Severity, ViolationKind

__all__ = [
    "CHECK_NAME",
    "DRCError",
    "DRCResult",
    "DRCStatus",
    "DRCViolation",
    "Severity",
    "ViolationKind",
    "run_geometry_check",
]
