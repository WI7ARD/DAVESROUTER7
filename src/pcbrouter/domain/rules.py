"""Design-rule containers.

Stage 1 only records rules that the board file states explicitly. KiCad 6+ keeps
most design rules in the ``.kicad_pro`` project file and ``.kicad_dru`` rule files;
reading those is planned for the routing-constraint stage. Absent values stay
``None`` so the UI shows "unknown" instead of an invented default.
"""

from __future__ import annotations

from dataclasses import dataclass

from pcbrouter.domain.units import Nm


@dataclass(frozen=True, slots=True)
class DesignRules:
    source: str = "unknown"  # where the values came from, e.g. "board setup"
    min_clearance: Nm | None = None
    min_track_width: Nm | None = None
    min_via_diameter: Nm | None = None
    min_via_drill: Nm | None = None
    board_thickness: Nm | None = None

    @property
    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (
                self.min_clearance,
                self.min_track_width,
                self.min_via_diameter,
                self.min_via_drill,
                self.board_thickness,
            )
        )
