"""Design-rule *data* as stated by the source files.

This module only holds values; interpreting them (precedence, fallbacks, sources)
is the job of :mod:`pcbrouter.rules`. Absent values stay ``None`` so the UI shows
"unknown" and the rule engine can refuse to guess (conservative handling).

Where values come from:

* KiCad 5 boards keep minimums and net classes inside the ``.kicad_pcb`` file;
* KiCad 6+ keeps them in the ``.kicad_pro`` project file (and custom rules in
  ``.kicad_dru``), which :mod:`pcbrouter.kicad.rule_adapter` reads read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from pcbrouter.domain.units import Nm


@dataclass(frozen=True, slots=True)
class DesignRules:
    """Board-wide minimum (fabrication) constraints. ``None`` = not stated."""

    source: str = "unknown"  # where the values came from, e.g. "board setup"
    min_clearance: Nm | None = None
    min_track_width: Nm | None = None
    min_via_diameter: Nm | None = None
    min_via_drill: Nm | None = None
    board_thickness: Nm | None = None
    min_copper_edge_clearance: Nm | None = None
    min_hole_clearance: Nm | None = None  # copper to hole
    min_hole_to_hole: Nm | None = None
    min_via_annular_width: Nm | None = None
    min_through_hole_diameter: Nm | None = None  # smallest drill of any kind
    min_microvia_diameter: Nm | None = None
    min_microvia_drill: Nm | None = None

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self) if f.name != "source")

    def merged(self, other: DesignRules) -> DesignRules:
        """Values from ``other`` win where they are stated."""
        values = {
            f.name: (
                getattr(other, f.name)
                if getattr(other, f.name) is not None
                else getattr(self, f.name)
            )
            for f in fields(self)
            if f.name != "source"
        }
        sources = [s for s in (self.source, other.source) if s and s != "unknown"]
        return DesignRules(source=" + ".join(dict.fromkeys(sources)) or "unknown", **values)


@dataclass(frozen=True, slots=True)
class NetClassDef:
    """A net class as stated by the source files. Unknown values stay ``None``."""

    name: str
    clearance: Nm | None = None
    track_width: Nm | None = None
    via_diameter: Nm | None = None
    via_drill: Nm | None = None
    microvia_diameter: Nm | None = None
    microvia_drill: Nm | None = None
    diff_pair_width: Nm | None = None
    diff_pair_gap: Nm | None = None
    description: str | None = None
    #: Nets explicitly listed as members (KiCad 5 ``add_net`` / KiCad 6 ``nets``).
    nets: tuple[str, ...] = ()
    source: str = "unknown"


@dataclass(frozen=True, slots=True)
class NetClassPattern:
    """KiCad 7+ ``netclass_patterns`` entry: nets whose name matches ``pattern``
    (``*``/``?`` wildcards) belong to ``netclass``."""

    pattern: str
    netclass: str
