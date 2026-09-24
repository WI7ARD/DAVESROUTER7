"""Semantic validation: a schema-valid command is not necessarily a PCB-valid one.

The validator is deterministic and has the final word. AI confidence never
overrides it; an invalid "high confidence" command stays invalid.

It also *resolves* identifiers: anonymised tokens are mapped back to real names by
exact lookup. Unknown names are rejected with suggestions (fuzzy matching is used
**only** to suggest; nothing is ever substituted automatically).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pcbrouter.ai.anonymizer import Anonymizer, EntityKind
from pcbrouter.ai.command_schema import (
    AICommand,
    AreaTarget,
    ComponentTarget,
    NetGroupTarget,
    NetTarget,
    Operation,
    OperationCategory,
    RoutingConstraints,
)
from pcbrouter.domain.board import Board
from pcbrouter.domain.units import internal_to_mm


class ValidationStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_WARNINGS = "valid_with_warnings"
    INVALID = "invalid"

    @property
    def label(self) -> str:
        return {
            "valid": "VALID",
            "valid_with_warnings": "VALID WITH WARNINGS",
            "invalid": "INVALID",
        }[self.value]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    suggestions: tuple[str, ...] = ()

    def text(self) -> str:
        if self.suggestions:
            return f"{self.message} Did you mean: {', '.join(self.suggestions)}?"
        return self.message


@dataclass(frozen=True, slots=True)
class CheckResult:
    label: str
    passed: bool


@dataclass(frozen=True, slots=True)
class ValidationReport:
    status: ValidationStatus
    issues: tuple[ValidationIssue, ...]
    checks: tuple[CheckResult, ...]
    #: The command with anonymised identifiers mapped back to real names.
    resolved: AICommand | None

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def is_valid(self) -> bool:
        return self.status is not ValidationStatus.INVALID


@dataclass
class SessionLocks:
    """Locks approved during this AI session (constraint state, not geometry)."""

    components: set[str] = field(default_factory=set)
    nets: set[str] = field(default_factory=set)
    track_nets: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Rule:
    allowed: frozenset[str]
    min_targets: int = 1
    max_targets: int = 64


_NETS = frozenset({"net", "net_group"})
RULES: dict[Operation, _Rule] = {
    Operation.ANALYZE_BOARD: _Rule(frozenset({"board"}), 0, 1),
    Operation.ANALYZE_NET: _Rule(_NETS),
    Operation.ANALYZE_COMPONENT: _Rule(frozenset({"component"})),
    Operation.EXPLAIN_ROUTE: _Rule(_NETS),
    Operation.ROUTE_NET: _Rule(frozenset({"net"}), 1, 1),
    Operation.ROUTE_GROUP: _Rule(_NETS),
    Operation.ROUTE_BOARD: _Rule(frozenset({"board"}), 0, 1),
    Operation.OPTIMIZE_NET: _Rule(_NETS | {"board"}),
    Operation.REDUCE_VIAS: _Rule(_NETS | {"board"}),
    Operation.SET_NET_CONSTRAINT: _Rule(_NETS),
    Operation.SET_ROUTING_PRIORITY: _Rule(_NETS),
    Operation.LOCK_COMPONENT: _Rule(frozenset({"component"})),
    Operation.LOCK_NET: _Rule(_NETS),
    Operation.LOCK_TRACK: _Rule(_NETS),
    Operation.PROTECT_AREA: _Rule(frozenset({"area"})),
}

_NORMALISE_RE = re.compile(r"[^0-9a-z]+")


def suggest(name: str, candidates: list[str], limit: int = 3) -> tuple[str, ...]:
    """Similar names for a message. Suggestions only — never auto-substituted."""
    norm = _NORMALISE_RE.sub("", name.casefold())
    exactish = [c for c in candidates if _NORMALISE_RE.sub("", c.casefold()) == norm and c != name]
    close = difflib.get_close_matches(name, candidates, n=limit, cutoff=0.6)
    upper = difflib.get_close_matches(
        name.upper(), [c.upper() for c in candidates], n=limit, cutoff=0.6
    )
    by_upper = {c.upper(): c for c in candidates}
    ordered: list[str] = []
    for c in [*exactish, *close, *(by_upper[u] for u in upper)]:
        if c not in ordered and c != name:
            ordered.append(c)
    return tuple(ordered[:limit])


class SemanticValidator:
    def __init__(
        self,
        board: Board,
        anonymizer: Anonymizer | None = None,
        locks: SessionLocks | None = None,
    ) -> None:
        self.board = board
        self.anonymizer = anonymizer
        self.locks = locks or SessionLocks()
        self._nets = [n.name for n in board.nets]
        self._refs = sorted(board.index.components_by_ref)
        self._copper = set(board.copper_layer_names)
        self._all_layers = {lyr.name for lyr in board.layers}

    # ------------------------------------------------------------------ public
    def validate(self, command: AICommand) -> ValidationReport:
        issues: list[ValidationIssue] = []
        failed: set[str] = set()

        def add(
            sev: Severity,
            code: str,
            msg: str,
            check: str | None = None,
            suggestions: tuple[str, ...] = (),
        ) -> None:
            issues.append(ValidationIssue(sev, code, msg, suggestions))
            if sev is Severity.ERROR and check:
                failed.add(check)

        resolved = self._resolve(command, add)
        cmd = resolved or command
        rule = RULES[cmd.operation]
        c = cmd.effective_constraints

        # --- targets vs operation
        kinds = [t.type for t in cmd.targets]
        for k in kinds:
            if k not in rule.allowed:
                add(
                    Severity.ERROR,
                    "target_type",
                    f"{cmd.operation.label} does not accept a '{k}' target "
                    f"(allowed: {', '.join(sorted(rule.allowed))}).",
                    "op",
                )
        if not rule.min_targets <= len(cmd.targets) <= rule.max_targets:
            add(
                Severity.ERROR,
                "target_count",
                f"{cmd.operation.label} needs {rule.min_targets}"
                + (f"–{rule.max_targets}" if rule.max_targets != rule.min_targets else "")
                + f" target(s); got {len(cmd.targets)}.",
                "op",
            )
        target_nets = self._target_nets(cmd)
        if cmd.operation is Operation.ROUTE_GROUP and len(set(target_nets)) < 2:
            add(Severity.ERROR, "group_size", "Route Group needs at least two nets.", "op")
        if len(target_nets) != len(set(target_nets)):
            add(Severity.ERROR, "duplicate_net", "The same net is targeted more than once.", "op")

        # --- existence
        for net in dict.fromkeys(target_nets):
            self._check_net(net, "Target net", add)
        for t in cmd.targets:
            if isinstance(t, ComponentTarget):
                self._check_ref(t.reference, "Target component", add)
            if isinstance(t, AreaTarget):
                self._check_layers(t.layers or [], "Protected-area layer", add)
                self._check_area(t, add)

        # --- operation-specific requirements
        if cmd.operation is Operation.SET_NET_CONSTRAINT and c.is_empty():
            add(
                Severity.ERROR,
                "no_constraints",
                "Set Net Constraint needs at least one constraint.",
                "constraints",
            )
        if cmd.operation is Operation.SET_ROUTING_PRIORITY and c.priority is None:
            add(
                Severity.ERROR,
                "no_priority",
                "Set Routing Priority needs constraints.priority.",
                "constraints",
            )

        self._check_constraints(cmd, c, target_nets, add)
        self._check_locks(cmd, c, target_nets, add)
        self._stage_notes(cmd, c, target_nets, add)

        if cmd.confidence == "low":
            add(
                Severity.WARNING,
                "low_confidence",
                "The AI reported low confidence in this proposal; review it carefully.",
            )

        status = (
            ValidationStatus.INVALID
            if any(i.severity is Severity.ERROR for i in issues)
            else (
                ValidationStatus.VALID_WITH_WARNINGS
                if any(i.severity is Severity.WARNING for i in issues)
                else ValidationStatus.VALID
            )
        )
        checks = (
            CheckResult("Operation accepts these targets", "op" not in failed),
            CheckResult("Referenced nets and components exist", "entities" not in failed),
            CheckResult("Layers exist and are copper", "layers" not in failed),
            CheckResult("Constraints are consistent", "constraints" not in failed),
            CheckResult("Locked items are respected", "locks" not in failed),
        )
        return ValidationReport(status, tuple(issues), checks, resolved)

    # ------------------------------------------------------------------ resolution
    def _resolve(self, command: AICommand, add: Any) -> AICommand | None:
        anon = self.anonymizer
        if anon is None or not anon.options.any:
            return command

        def net(name: str) -> str:
            r = anon.resolve(EntityKind.NET, name)
            if r.problem:
                add(Severity.ERROR, "ambiguous", r.problem, "entities")
            if r.note:
                add(Severity.INFO, "unanonymised", r.note)
            return r.real or name

        def ref(name: str) -> str:
            r = anon.resolve(EntityKind.REFERENCE, name)
            if r.problem:
                add(Severity.ERROR, "ambiguous", r.problem, "entities")
            return r.real or name

        data = command.model_dump(mode="json", exclude_none=True)
        for t in data.get("targets", []):
            if t["type"] == "net":
                t["name"] = net(t["name"])
            elif t["type"] == "net_group":
                t["names"] = [net(n) for n in t["names"]]
            elif t["type"] == "component":
                t["reference"] = ref(t["reference"])
        cons = data.get("constraints", {})
        if "avoid_nets" in cons:
            cons["avoid_nets"] = [net(n) for n in cons["avoid_nets"]]
        for key in ("keep_near", "keep_away_from"):
            for e in cons.get(key, []):
                e["name"] = net(e["name"]) if e["kind"] == "net" else ref(e["name"])
        if dp := cons.get("differential_pair"):
            dp["positive_net"] = net(dp["positive_net"])
            dp["negative_net"] = net(dp["negative_net"])
        try:
            return AICommand.model_validate(data)
        except Exception as exc:  # e.g. two tokens mapped onto duplicate names
            add(
                Severity.ERROR,
                "resolution",
                f"Identifiers could not be resolved: {exc}",
                "entities",
            )
            return None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _target_nets(cmd: AICommand) -> list[str]:
        nets: list[str] = []
        for t in cmd.targets:
            if isinstance(t, NetTarget):
                nets.append(t.name)
            elif isinstance(t, NetGroupTarget):
                nets.extend(t.names)
        return nets

    def _check_net(self, name: str, what: str, add: Any) -> bool:
        if name in self.board.index.nets_by_name:
            return True
        add(
            Severity.ERROR,
            "unknown_net",
            f'{what} "{name}" does not exist on this board.',
            "entities",
            suggest(name, self._nets),
        )
        return False

    def _check_ref(self, ref: str, what: str, add: Any) -> bool:
        if ref in self.board.index.components_by_ref:
            return True
        add(
            Severity.ERROR,
            "unknown_component",
            f'{what} "{ref}" does not exist on this board.',
            "entities",
            suggest(ref, self._refs),
        )
        return False

    def _check_layers(self, layers: list[str], what: str, add: Any) -> None:
        for layer in layers:
            if layer in self._copper:
                continue
            if layer in self._all_layers:
                add(
                    Severity.ERROR,
                    "not_copper",
                    f'{what} "{layer}" exists but is not a copper layer; it cannot carry routing.',
                    "layers",
                )
            else:
                add(
                    Severity.ERROR,
                    "unknown_layer",
                    f'{what} "{layer}" does not exist on this board.',
                    "layers",
                    suggest(layer, sorted(self._copper)),
                )

    def _check_area(self, area: AreaTarget, add: Any) -> None:
        b = self.board.bounds
        if b is None:
            return
        inside = (
            area.x_min_mm < internal_to_mm(b.max_x)
            and area.x_max_mm > internal_to_mm(b.min_x)
            and area.y_min_mm < internal_to_mm(b.max_y)
            and area.y_max_mm > internal_to_mm(b.min_y)
        )
        if not inside:
            add(
                Severity.WARNING,
                "area_outside",
                "The protected area lies outside the board bounds.",
            )

    def _check_constraints(
        self, cmd: AICommand, c: RoutingConstraints, target_nets: list[str], add: Any
    ) -> None:
        self._check_layers(c.preferred_layers or [], "Preferred layer", add)
        self._check_layers(c.forbidden_layers or [], "Forbidden layer", add)
        overlap = set(c.preferred_layers or []) & set(c.forbidden_layers or [])
        if overlap:
            add(
                Severity.ERROR,
                "layer_conflict",
                f"Layers both preferred and forbidden: {', '.join(sorted(overlap))}.",
                "constraints",
            )
        if c.forbidden_layers and self._copper and set(c.forbidden_layers) >= self._copper:
            add(
                Severity.ERROR,
                "all_layers_forbidden",
                "Every copper layer is forbidden.",
                "constraints",
            )

        w_min, w_pref, w_max = (
            c.min_trace_width_mm,
            c.preferred_trace_width_mm,
            c.max_trace_width_mm,
        )
        if w_min is not None and w_max is not None and w_min > w_max:
            add(
                Severity.ERROR, "width_order", "Minimum trace width exceeds maximum.", "constraints"
            )
        if w_pref is not None and (
            (w_min is not None and w_pref < w_min) or (w_max is not None and w_pref > w_max)
        ):
            add(
                Severity.ERROR,
                "width_order",
                "Preferred trace width is outside min/max.",
                "constraints",
            )
        rules = self.board.rules
        for label, value in (("Minimum", w_min), ("Preferred", w_pref)):
            if (
                value is not None
                and rules.min_track_width is not None
                and value < internal_to_mm(rules.min_track_width)
            ):
                add(
                    Severity.WARNING,
                    "below_rule",
                    f"{label} trace width {value} mm is below the board's minimum "
                    f"{internal_to_mm(rules.min_track_width)} mm.",
                )
        if (
            c.min_clearance_mm is not None
            and rules.min_clearance is not None
            and c.min_clearance_mm < internal_to_mm(rules.min_clearance)
        ):
            add(
                Severity.WARNING,
                "below_rule",
                f"Clearance {c.min_clearance_mm} mm is below the board rule "
                f"{internal_to_mm(rules.min_clearance)} mm; board rules still apply.",
            )

        if c.target_length_mm is not None:
            if c.max_length_mm is not None and c.target_length_mm > c.max_length_mm:
                add(
                    Severity.ERROR,
                    "length_order",
                    "Target length exceeds the maximum length.",
                    "constraints",
                )
            if c.length_tolerance_mm is None:
                add(Severity.WARNING, "no_tolerance", "Target length has no tolerance.")
            elif c.length_tolerance_mm >= c.target_length_mm:
                add(
                    Severity.ERROR,
                    "tolerance",
                    "Length tolerance must be smaller than the target.",
                    "constraints",
                )
        elif c.length_tolerance_mm is not None:
            add(
                Severity.WARNING,
                "orphan_tolerance",
                "Length tolerance given without a target length.",
            )

        if c.preserve_existing_routes is True and c.allow_ripup is True:
            add(
                Severity.ERROR,
                "ripup_conflict",
                "Conflicting constraints: preserve existing routes AND allow rip-up.",
                "constraints",
            )

        if c.preferred_via_type in ("blind_buried", "micro") and len(self._copper) < 4:
            add(
                Severity.ERROR,
                "via_type",
                f"{c.preferred_via_type} vias need inner layers; this board has "
                f"{len(self._copper)} copper layers.",
                "constraints",
            )

        for name in c.avoid_nets or []:
            self._check_net(name, "Avoided net", add)
            if name in target_nets:
                add(
                    Severity.ERROR,
                    "avoid_target",
                    f'Net "{name}" is both a target and avoided.',
                    "constraints",
                )
        near = {(e.kind, e.name) for e in c.keep_near or []}
        away = {(e.kind, e.name) for e in c.keep_away_from or []}
        for kind, name in near | away:
            if kind == "net":
                self._check_net(name, "Referenced net", add)
            else:
                self._check_ref(name, "Referenced component", add)
        if near & away:
            add(
                Severity.ERROR,
                "near_away",
                "An entity is in both keep_near and keep_away_from.",
                "constraints",
            )

        if c.avoid_net_classes:
            add(
                Severity.WARNING,
                "net_classes_unknown",
                "Net classes ("
                + ", ".join(c.avoid_net_classes)
                + ") cannot be verified: net-class "
                "data is not loaded yet. The constraint is recorded but unchecked.",
            )
        dp = c.differential_pair
        if dp is not None:
            for n in (dp.positive_net, dp.negative_net):
                self._check_net(n, "Differential-pair net", add)
                if target_nets and n not in target_nets:
                    add(
                        Severity.WARNING,
                        "pair_not_target",
                        f'Differential-pair net "{n}" is not among the command targets.',
                    )
            add(
                Severity.WARNING,
                "pair_rules_unavailable",
                "Differential pair rules are not yet available from deterministic geometry "
                "rules (planned for Stage 6); the pair intent is recorded only.",
            )
        elif c.pair_gap_mm is not None or c.pair_skew_tolerance_mm is not None:
            add(Severity.WARNING, "pair_params", "Pair gap/skew given without a differential pair.")
        if c.impedance_target_ohm is not None:
            add(
                Severity.WARNING,
                "impedance",
                "Impedance targets need stackup data that is not available; recorded only.",
            )

    def _check_locks(
        self, cmd: AICommand, c: RoutingConstraints, target_nets: list[str], add: Any
    ) -> None:
        idx = self.board.index
        routing = cmd.operation.category is OperationCategory.ROUTING
        if c.effective_allow_component_movement:
            for t in cmd.targets:
                if isinstance(t, ComponentTarget) and self._is_locked_component(t.reference):
                    add(
                        Severity.ERROR,
                        "locked_component",
                        f"Component {t.reference} is locked and cannot be moved.",
                        "locks",
                    )
            locked_on_nets = sorted(
                {
                    p.footprint_ref
                    for n in target_nets
                    for p in idx.pads_by_net.get(n, [])
                    if self._is_locked_component(p.footprint_ref)
                }
            )
            if locked_on_nets:
                add(
                    Severity.WARNING,
                    "locked_on_nets",
                    "Locked components on these nets will not be moved: "
                    + ", ".join(locked_on_nets)
                    + ".",
                )
            if cmd.operation is Operation.ROUTE_BOARD:
                add(
                    Severity.WARNING,
                    "movement_board",
                    "Component movement is allowed board-wide; locked parts will stay fixed.",
                )
        if routing:
            for net in target_nets:
                if net in self.locks.nets:
                    add(
                        Severity.ERROR,
                        "locked_net",
                        f'Net "{net}" was locked earlier in this session.',
                        "locks",
                    )
                touches_copper = (
                    not c.effective_preserve_existing_routes
                ) or c.effective_allow_ripup
                locked_tracks = any(t.locked for t in idx.tracks_by_net.get(net, []))
                if touches_copper and (locked_tracks or net in self.locks.track_nets):
                    add(
                        Severity.ERROR,
                        "locked_tracks",
                        f'Net "{net}" has locked tracks, which rip-up/replacement would modify.',
                        "locks",
                    )
        if cmd.operation is Operation.LOCK_COMPONENT:
            for t in cmd.targets:
                if isinstance(t, ComponentTarget) and self._is_locked_component(t.reference):
                    add(
                        Severity.INFO,
                        "already_locked",
                        f"Component {t.reference} is already locked.",
                    )

    def _is_locked_component(self, ref: str) -> bool:
        comp = self.board.index.components_by_ref.get(ref)
        return ref in self.locks.components or (comp is not None and comp.footprint.locked)

    def _stage_notes(
        self, cmd: AICommand, c: RoutingConstraints, target_nets: list[str], add: Any
    ) -> None:
        idx = self.board.index
        if cmd.operation.category is OperationCategory.ROUTING:
            add(
                Severity.INFO,
                "stage",
                "Routing execution becomes available in Stage 4. "
                "Approving records the intent only; no copper changes.",
            )
            for net in dict.fromkeys(target_nets):
                st = idx.net_statistics.get(net)
                if st is None:
                    continue
                if st.pad_count < 2:
                    add(
                        Severity.WARNING,
                        "few_pads",
                        f'Net "{net}" has {st.pad_count} pad(s); there is nothing to connect.',
                    )
                if st.track_count and not c.effective_preserve_existing_routes:
                    add(
                        Severity.WARNING,
                        "replace_routes",
                        f'Net "{net}" already has {st.track_count} track(s) that may be replaced.',
                    )
        if cmd.operation is Operation.LOCK_TRACK:
            for net in target_nets:
                if not idx.tracks_by_net.get(net):
                    add(Severity.WARNING, "no_tracks", f'Net "{net}" has no tracks to lock.')
        if cmd.operation is Operation.ROUTE_BOARD and c.is_empty():
            add(
                Severity.INFO,
                "defaults",
                "No constraints given: safe defaults apply (preserve "
                "existing routes, no rip-up, no component movement).",
            )
