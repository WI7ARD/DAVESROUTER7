"""``rules_snapshot.json``: the normalised, resolved rule set tied to a board.

For debugging, AI context and reproducibility. Contains design-rule values, class
membership and sources — no secrets, no copper geometry. Written only on explicit
user action (Tools ▸ Export Rules Snapshot).
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from pcbrouter import __version__
from pcbrouter.domain.board import Board
from pcbrouter.domain.units import Nm, internal_to_mm
from pcbrouter.rules.model import ResolvedValue
from pcbrouter.rules.resolver import RuleResolver

SNAPSHOT_FORMAT = 1


def _mm(v: Nm | None) -> float | None:
    return internal_to_mm(v) if v is not None else None


def _rv(v: ResolvedValue) -> dict[str, Any]:
    out: dict[str, Any] = {"mm": _mm(v.value), "source": v.source.describe()}
    if v.possibly_stricter is not None:
        out["possibly_stricter_mm"] = _mm(v.possibly_stricter)
        out["possibly_stricter_rules"] = list(v.possibly_stricter_rules)
    if v.notes:
        out["notes"] = list(v.notes)
    return out


def rules_snapshot(resolver: RuleResolver, board: Board) -> dict[str, Any]:
    rs = resolver.ruleset
    board_rules = {
        f.name: _mm(getattr(rs.board, f.name)) for f in fields(rs.board) if f.name != "source"
    }
    classes: dict[str, Any] = {}
    for name in sorted(rs.classes.classes):
        c = rs.classes.rules_for_class(name)
        classes[name] = {
            "clearance": _rv(c.clearance),
            "track_width": _rv(c.track_width),
            "via_diameter": _rv(c.via_diameter),
            "via_drill": _rv(c.via_drill),
        }
    nets: dict[str, Any] = {}
    for net in sorted(n.name for n in board.nets if n.name):
        s = resolver.summary(net)
        nets[net] = {
            "net_classes": list(s.net_classes),
            "class_source": s.class_source.describe(),
            "preferred_width": _rv(s.width.preferred),
            "min_width": _rv(s.width.minimum),
            "fab_min_width": _rv(s.width.fab_minimum),
            "clearance": _rv(s.clearance),
            "via_diameter": _rv(s.via.diameter),
            "via_drill": _rv(s.via.drill),
            "allowed_layers": list(s.layers.allowed),
            "max_vias": s.max_vias.value,
        }
    return {
        "format": SNAPSHOT_FORMAT,
        "app_version": __version__,
        "rule_engine_version": rs.version,
        "board_fingerprint": board.fingerprint,
        "rules_digest": rs.digest,
        "conservative_rule_handling": resolver.conservative,
        "sources": dict(rs.sources),
        "board_defaults": {"source": rs.board.source, **board_rules},
        "edge_clearance": _rv(resolver.resolve_edge_clearance()),
        "hole_to_hole": _rv(resolver.resolve_hole_to_hole()),
        "net_classes": classes,
        "netclass_patterns": [
            {"pattern": p.pattern, "netclass": p.netclass} for p in rs.classes.patterns
        ],
        "custom_rules": [
            {
                "name": r.name,
                "location": r.location,
                "condition": r.condition.text if r.condition else None,
                "layers": sorted(r.layers) if r.layers is not None else None,
                "constraints": [
                    {
                        "kind": c.kind,
                        "min_mm": _mm(c.min),
                        "opt_mm": _mm(c.opt),
                        "max_mm": _mm(c.max),
                        "items": list(c.items),
                    }
                    for c in r.constraints
                ],
            }
            for r in rs.custom_rules
        ],
        "unsupported_rules": [
            {"rule": u.describe(), "critical": u.critical} for u in rs.unsupported
        ],
        "ignored_rules": list(rs.ignored),
        "overrides": resolver.overrides.to_dict(),
        "nets": nets,
        "warnings": list(rs.warnings),
    }
