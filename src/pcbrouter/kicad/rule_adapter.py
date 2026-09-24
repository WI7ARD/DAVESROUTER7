"""Read design rules from the KiCad project files next to a board (read-only).

KiCad 6+ keeps most rules outside the ``.kicad_pcb``:

* ``<board>.kicad_pro`` (JSON): board minimums (``board.design_settings.rules``),
  net classes (``net_settings.classes``), and KiCad 7+ class assignments
  (``netclass_patterns`` / ``netclass_assignments``);
* ``<board>.kicad_dru`` (S-expressions): custom rules with conditions.

This module only *reads and normalises* those files into KiCad-neutral data
(:class:`ProjectRuleData`). Interpreting rules — precedence, condition evaluation,
conservative fallbacks — is the job of :mod:`pcbrouter.rules`. Files are opened in
binary read mode only; their SHA-256 is recorded for the rules snapshot.

Problems never abort board loading: an unreadable project file yields warnings and
leaves the corresponding rules *unknown* (never invented).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pcbrouter.domain.rules import DesignRules, NetClassDef, NetClassPattern
from pcbrouter.domain.units import NM_PER_MIL, NM_PER_MM, Nm, mm_to_internal
from pcbrouter.kicad.errors import MalformedBoardError
from pcbrouter.kicad.parser import SNode, line_col, parse_sexpr

log = logging.getLogger(__name__)

#: Refuse absurd project files (real ones are a few hundred kB at most).
MAX_RULE_FILE_BYTES = 16 * 1024 * 1024

# .kicad_pro design_settings.rules key -> DesignRules field
_PRO_RULE_KEYS: dict[str, str] = {
    "min_clearance": "min_clearance",
    "min_track_width": "min_track_width",
    "min_via_diameter": "min_via_diameter",
    "min_copper_edge_clearance": "min_copper_edge_clearance",
    "min_hole_clearance": "min_hole_clearance",
    "min_hole_to_hole": "min_hole_to_hole",
    "min_via_annular_width": "min_via_annular_width",
    "min_through_hole_diameter": "min_through_hole_diameter",
    "min_microvia_diameter": "min_microvia_diameter",
    "min_microvia_drill": "min_microvia_drill",
}
# .kicad_pro net class key -> NetClassDef field
_PRO_CLASS_KEYS: dict[str, str] = {
    "clearance": "clearance",
    "track_width": "track_width",
    "via_diameter": "via_diameter",
    "via_drill": "via_drill",
    "microvia_diameter": "microvia_diameter",
    "microvia_drill": "microvia_drill",
    "diff_pair_width": "diff_pair_width",
    "diff_pair_gap": "diff_pair_gap",
}


@dataclass(frozen=True, slots=True)
class ConstraintSpec:
    """One ``(constraint ...)`` of a custom rule, values already in nanometres."""

    kind: str  # e.g. "clearance", "track_width", "disallow"
    min: Nm | None = None
    opt: Nm | None = None
    max: Nm | None = None
    #: For ``disallow``: the item types, e.g. ("via",) or ("track", "via").
    items: tuple[str, ...] = ()
    #: Tokens this adapter could not interpret (value expressions, unknown units).
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CustomRuleSpec:
    """A ``(rule ...)`` from ``.kicad_dru`` as written (condition not yet compiled)."""

    name: str
    constraints: tuple[ConstraintSpec, ...]
    condition: str | None = None
    layer: str | None = None
    severity: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectRuleData:
    """Everything read from ``.kicad_pro`` / ``.kicad_dru``. Empty when absent."""

    project_path: Path | None = None
    project_sha256: str | None = None
    dru_path: Path | None = None
    dru_sha256: str | None = None
    design_rules: DesignRules = field(default_factory=DesignRules)
    net_classes: tuple[NetClassDef, ...] = ()
    patterns: tuple[NetClassPattern, ...] = ()
    #: net name -> class name(s); KiCad 9 allows several classes per net.
    assignments: MappingProxyType[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    custom_rules: tuple[CustomRuleSpec, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def found_any(self) -> bool:
        return self.project_path is not None or self.dru_path is not None


def _read_limited(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_RULE_FILE_BYTES:
        raise OSError(f"{path.name} is too large ({size} bytes)")
    with path.open("rb") as fh:  # read-only, binary
        return fh.read()


def _mm_value(value: Any) -> Nm | None:
    """A JSON millimetre number -> nm (``None`` for missing / non-numeric)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return mm_to_internal(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


_DRU_VALUE_RE = re.compile(r"^\s*(-?\d+(?:\.\d*)?|-?\.\d+)\s*(mm|mil|mils|in|um|µm|nm)?\s*$")


def parse_dru_length(text: str) -> Nm | None:
    """``"0.2mm"``, ``"8mil"``, ``"0.01in"``, ``"0.2"`` (mm) -> nm; ``None`` if the text
    is an expression or uses an unknown unit (the caller records it as a problem)."""
    match = _DRU_VALUE_RE.match(text)
    if not match:
        return None
    number = Decimal(match.group(1))
    unit = (match.group(2) or "mm").lower()
    scale = {
        "mm": Decimal(NM_PER_MM),
        "mil": Decimal(NM_PER_MIL),
        "mils": Decimal(NM_PER_MIL),
        "in": Decimal(NM_PER_MIL * 1000),
        "um": Decimal(1000),
        "µm": Decimal(1000),
        "nm": Decimal(1),
    }[unit]
    return int((number * scale).to_integral_value())


# ------------------------------------------------------------------ .kicad_pro
def _parse_project(data: bytes, path: Path, warnings: list[str]) -> dict[str, Any]:
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        warnings.append(f"{path.name}: not valid JSON ({exc}); project rules unknown")
        return {}
    if not isinstance(doc, dict):
        warnings.append(f"{path.name}: unexpected content; project rules unknown")
        return {}
    return doc


def _project_design_rules(doc: dict[str, Any], path: Path) -> DesignRules:
    rules = (
        doc.get("board", {}).get("design_settings", {}).get("rules", {})
        if isinstance(doc.get("board"), dict)
        else {}
    )
    if not isinstance(rules, dict):
        return DesignRules()
    values: dict[str, Nm | None] = {}
    for key, attr in _PRO_RULE_KEYS.items():
        if key in rules:
            values[attr] = _mm_value(rules[key])
    if not values:
        return DesignRules()
    return DesignRules(source=f"project file ({path.name})", **values)


def _project_net_classes(
    doc: dict[str, Any], path: Path, warnings: list[str]
) -> tuple[tuple[NetClassDef, ...], tuple[NetClassPattern, ...], dict[str, tuple[str, ...]]]:
    settings = doc.get("net_settings")
    if not isinstance(settings, dict):
        return (), (), {}
    classes: list[NetClassDef] = []
    for entry in settings.get("classes") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            warnings.append(f"{path.name}: unreadable net class entry ignored")
            continue
        values = {attr: _mm_value(entry.get(key)) for key, attr in _PRO_CLASS_KEYS.items()}
        nets = entry.get("nets") or []
        classes.append(
            NetClassDef(
                name=entry["name"],
                description=entry.get("description") if isinstance(entry, dict) else None,
                nets=tuple(n for n in nets if isinstance(n, str)),
                source=f"project file ({path.name})",
                **values,
            )
        )
    patterns = tuple(
        NetClassPattern(str(p["pattern"]), str(p["netclass"]))
        for p in settings.get("netclass_patterns") or []
        if isinstance(p, dict) and "pattern" in p and "netclass" in p
    )
    assignments: dict[str, tuple[str, ...]] = {}
    raw = settings.get("netclass_assignments")
    if isinstance(raw, dict):
        for net, cls in raw.items():
            if isinstance(cls, str):
                assignments[str(net)] = (cls,)
            elif isinstance(cls, list):
                assignments[str(net)] = tuple(str(c) for c in cls)
    return tuple(classes), patterns, assignments


# ------------------------------------------------------------------ .kicad_dru
def _constraint(node: SNode) -> ConstraintSpec:
    kind = node.atom(0) or "?"
    if kind == "disallow":
        return ConstraintSpec(kind, items=tuple(node.atoms()[1:]))
    problems: list[str] = []
    values: dict[str, Nm | None] = {"min": None, "opt": None, "max": None}
    for key in values:
        text = node.value(key)
        if text is None:
            continue
        parsed = parse_dru_length(text)
        if parsed is None:
            problems.append(f"{key} value {text!r} not understood")
        values[key] = parsed
    extra = [a for a in node.atoms()[1:]]
    if extra:
        problems.append(f"unexpected tokens {extra}")
    return ConstraintSpec(kind, values["min"], values["opt"], values["max"], (), tuple(problems))


def _parse_dru(data: bytes, path: Path, warnings: list[str]) -> tuple[CustomRuleSpec, ...]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        warnings.append(f"{path.name}: not UTF-8 text; custom rules unknown")
        return ()
    # A .kicad_dru is a sequence of top-level lists; wrap it so the parser sees one tree.
    try:
        root = parse_sexpr(f"(kicad_dru\n{text}\n)")  # same text as `wrapped` below
    except MalformedBoardError as exc:
        warnings.append(f"{path.name}: could not be parsed ({exc}); custom rules unknown")
        return ()
    wrapped = f"(kicad_dru\n{text}\n)"
    rules: list[CustomRuleSpec] = []
    for node in root.nodes("rule"):
        name = node.atom(0) or "(unnamed)"
        line = line_col(wrapped, node.offset)[0] - 1
        rules.append(
            CustomRuleSpec(
                name=name,
                constraints=tuple(_constraint(c) for c in node.nodes("constraint")),
                condition=node.value("condition"),
                layer=node.value("layer"),
                severity=node.value("severity"),
                line=line,
            )
        )
    return tuple(rules)


# ------------------------------------------------------------------ entry point
def load_project_rules(board_path: Path) -> ProjectRuleData:
    """Read ``<stem>.kicad_pro`` and ``<stem>.kicad_dru`` next to ``board_path``."""
    warnings: list[str] = []
    pro_path = board_path.with_suffix(".kicad_pro")
    dru_path = board_path.with_suffix(".kicad_dru")
    result: dict[str, Any] = {}
    if pro_path.is_file():
        try:
            data = _read_limited(pro_path)
        except OSError as exc:
            warnings.append(f"{pro_path.name}: could not be read ({exc}); project rules unknown")
        else:
            result["project_path"] = pro_path
            result["project_sha256"] = hashlib.sha256(data).hexdigest()
            doc = _parse_project(data, pro_path, warnings)
            result["design_rules"] = _project_design_rules(doc, pro_path)
            classes, patterns, assignments = _project_net_classes(doc, pro_path, warnings)
            result["net_classes"] = classes
            result["patterns"] = patterns
            result["assignments"] = MappingProxyType(assignments)
    if dru_path.is_file():
        try:
            data = _read_limited(dru_path)
        except OSError as exc:
            warnings.append(f"{dru_path.name}: could not be read ({exc}); custom rules unknown")
        else:
            result["dru_path"] = dru_path
            result["dru_sha256"] = hashlib.sha256(data).hexdigest()
            result["custom_rules"] = _parse_dru(data, dru_path, warnings)
    rules = ProjectRuleData(warnings=tuple(warnings), **result)
    log.info(
        "rules.project_files project=%s dru=%s classes=%d patterns=%d custom_rules=%d "
        "warnings=%d",
        rules.project_path.name if rules.project_path else None,
        rules.dru_path.name if rules.dru_path else None,
        len(rules.net_classes), len(rules.patterns), len(rules.custom_rules), len(warnings),
    )  # fmt: skip
    return rules
