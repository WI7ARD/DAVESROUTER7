"""Compile a :class:`RuleSet` from the board file and the project files.

Sources merged (board file first, project file second so it wins where both state a
value — KiCad 6+ ignores the old board-file values once a project file exists):

* ``Board.rules`` / ``Board.net_classes`` (KiCad 5 style, inside the .kicad_pcb);
* :class:`~pcbrouter.kicad.rule_adapter.ProjectRuleData` (.kicad_pro / .kicad_dru).

Custom rules are compiled into :class:`CompiledRule` objects. A rule is *unsupported*
when its condition, layer selector, values or constraint kinds fall outside what
the engine evaluates; unsupported rules are kept, listed, and — when critical —
make affected checks RULE_UNKNOWN (see :mod:`pcbrouter.rules.resolver`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from pcbrouter.domain.board import Board
from pcbrouter.domain.layer import BACK_COPPER, FRONT_COPPER
from pcbrouter.domain.rules import DesignRules
from pcbrouter.kicad.rule_adapter import CustomRuleSpec, ProjectRuleData
from pcbrouter.rules.conditions import Condition, ConditionError, parse_condition
from pcbrouter.rules.model import (
    CRITICAL_CONSTRAINTS,
    KNOWN_NONCRITICAL,
    SUPPORTED_CONSTRAINTS,
    Constraint,
    UnsupportedRule,
)
from pcbrouter.rules.netclass import NetClassTable

RULE_ENGINE_VERSION = "3.0.0"


@dataclass(frozen=True, slots=True)
class CompiledRule:
    name: str
    constraints: tuple[Constraint, ...]
    condition: Condition | None
    layers: frozenset[str] | None  # None = all layers
    order: int  # file order; later rules take precedence (KiCad semantics)
    location: str | None

    def applies_to_layer(self, layer: str | None) -> bool:
        return self.layers is None or layer is None or layer in self.layers

    def constraint(self, kind: str) -> Constraint | None:
        return next((c for c in self.constraints if c.kind == kind), None)


@dataclass
class RuleSet:
    board: DesignRules
    classes: NetClassTable
    custom_rules: list[CompiledRule]
    unsupported: list[UnsupportedRule]
    #: Rules deliberately disabled in the file (severity ignore).
    ignored: list[str]
    copper_layers: tuple[str, ...]
    sources: dict[str, str | None]  # file name -> sha256
    warnings: list[str] = field(default_factory=list)
    version: str = RULE_ENGINE_VERSION
    #: Per-file values, to report exactly which file stated a board minimum.
    board_file_rules: DesignRules = field(default_factory=DesignRules)
    project_rules: DesignRules = field(default_factory=DesignRules)

    def board_value_source(self, attr: str) -> str:
        if getattr(self.project_rules, attr) is not None:
            return self.project_rules.source
        return self.board_file_rules.source

    @property
    def critical_unsupported(self) -> list[UnsupportedRule]:
        return [u for u in self.unsupported if u.critical]

    def rules_with(self, kind: str) -> list[CompiledRule]:
        """Supported custom rules containing ``kind``, highest precedence first."""
        return sorted(
            (r for r in self.custom_rules if r.constraint(kind) is not None),
            key=lambda r: -r.order,
        )

    def unsupported_with(self, kind: str) -> list[UnsupportedRule]:
        return [u for u in self.unsupported if kind in u.constraint_kinds]

    @property
    def digest(self) -> str:
        """Hash of the normalised rule content (for caches and snapshots)."""
        blob = json.dumps(
            {
                "board": repr(self.board),
                "classes": sorted(repr(c) for c in self.classes.classes.values()),
                "patterns": [repr(p) for p in self.classes.patterns],
                "assignments": sorted(self.classes.assignments.items()),
                "custom": [
                    repr(
                        (
                            r.name,
                            r.constraints,
                            r.condition.text if r.condition else None,
                            sorted(r.layers or ()),
                        )
                    )
                    for r in self.custom_rules
                ],
                "unsupported": [u.describe() for u in self.unsupported],
            },
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _layer_selector(
    text: str | None, copper: tuple[str, ...]
) -> tuple[frozenset[str] | None, str | None]:
    if text is None:
        return None, None
    if text == "outer":
        return frozenset({FRONT_COPPER, BACK_COPPER}), None
    if text == "inner":
        return (
            frozenset(layer for layer in copper if layer not in (FRONT_COPPER, BACK_COPPER)),
            None,
        )
    if text in copper:
        return frozenset({text}), None
    return None, f"layer selector '{text}' not understood"


def _compile(
    spec: CustomRuleSpec, order: int, copper: tuple[str, ...], file_name: str
) -> CompiledRule | UnsupportedRule | str:
    location = f"{file_name}:{spec.line}" if spec.line is not None else file_name
    kinds = tuple(c.kind for c in spec.constraints)
    critical = any(k in CRITICAL_CONSTRAINTS for k in kinds)
    if spec.severity == "ignore":
        return f'"{spec.name}" (severity ignore) [{location}]'
    reasons: list[str] = []
    for c in spec.constraints:
        if c.kind not in SUPPORTED_CONSTRAINTS:
            if c.kind in KNOWN_NONCRITICAL:
                reasons.append(f"constraint '{c.kind}' is not evaluated by the geometry engine")
            else:
                reasons.append(f"constraint '{c.kind}' is not supported")
        if c.problems:
            reasons.extend(c.problems)
    condition: Condition | None = None
    if spec.condition:
        try:
            condition = parse_condition(spec.condition)
        except ConditionError as exc:
            reasons.append(f"condition not supported ({exc})")
    layers, layer_problem = _layer_selector(spec.layer, copper)
    if layer_problem:
        reasons.append(layer_problem)
    if reasons:
        return UnsupportedRule(
            spec.name,
            "; ".join(reasons),
            kinds,
            critical,
            location,
            limits=tuple(
                (c.kind, c.min if not c.problems else None, c.max) for c in spec.constraints
            ),
            disallow_items=tuple(
                i for c in spec.constraints if c.kind == "disallow" for i in c.items
            ),
        )
    constraints = tuple(Constraint(c.kind, c.min, c.opt, c.max, c.items) for c in spec.constraints)
    return CompiledRule(spec.name, constraints, condition, layers, order, location)


def build_ruleset(board: Board, project: ProjectRuleData | None = None) -> RuleSet:
    project = project or ProjectRuleData()
    board_rules = board.rules
    if not project.design_rules.is_empty:
        board_rules = board_rules.merged(project.design_rules)
    classes = NetClassTable(
        list(board.net_classes) + list(project.net_classes),
        project.patterns,
        project.assignments,
    )
    copper = tuple(board.copper_layer_names)
    compiled: list[CompiledRule] = []
    unsupported: list[UnsupportedRule] = []
    ignored: list[str] = []
    file_name = project.dru_path.name if project.dru_path else ".kicad_dru"
    for order, spec in enumerate(project.custom_rules):
        result = _compile(spec, order, copper, file_name)
        if isinstance(result, CompiledRule):
            compiled.append(result)
        elif isinstance(result, UnsupportedRule):
            unsupported.append(result)
        else:
            ignored.append(result)
    sources: dict[str, str | None] = {}
    if board.metadata.source_path is not None:
        sources[board.metadata.source_path.name] = None
    if project.project_path is not None:
        sources[project.project_path.name] = project.project_sha256
    if project.dru_path is not None:
        sources[project.dru_path.name] = project.dru_sha256
    return RuleSet(
        board=board_rules,
        classes=classes,
        custom_rules=compiled,
        unsupported=unsupported,
        ignored=ignored,
        copper_layers=copper,
        sources=sources,
        warnings=list(project.warnings),
        board_file_rules=board.rules,
        project_rules=project.design_rules,
    )
