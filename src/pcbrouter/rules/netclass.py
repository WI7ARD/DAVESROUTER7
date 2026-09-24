"""Net-class membership and per-class values.

Membership order (first that applies wins; matches KiCad 7-9 behaviour):

1. explicit ``netclass_assignments`` (KiCad 8+; KiCad 9 may list several classes);
2. explicit member lists (KiCad 5 ``add_net`` / KiCad 6 ``nets``);
3. ``netclass_patterns`` in file order (``*``/``?`` wildcards, whole-name match);
4. ``Default``.

Class values missing in a non-Default class inherit from ``Default`` (KiCad 9
semantics; older versions always write every value). A net in several classes
(KiCad 9) takes, for each value, the *strictest* (largest) one — conservative.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from pcbrouter.domain.rules import NetClassDef, NetClassPattern
from pcbrouter.rules.model import (
    NetClassRules,
    ResolvedValue,
    RuleSource,
    RuleSourceKind,
    unknown,
)

DEFAULT_CLASS = "Default"
_VALUE_FIELDS = (
    "clearance",
    "track_width",
    "via_diameter",
    "via_drill",
    "microvia_diameter",
    "microvia_drill",
)


@dataclass(frozen=True, slots=True)
class Membership:
    classes: tuple[str, ...]
    source: RuleSource


class NetClassTable:
    def __init__(
        self,
        classes: Sequence[NetClassDef],
        patterns: Sequence[NetClassPattern] = (),
        assignments: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        # Later definitions of the same class name win (project file after board file).
        self.classes: dict[str, NetClassDef] = {}
        for nc in classes:
            self.classes[nc.name] = nc
        self.patterns = tuple(patterns)
        self.assignments = dict(assignments or {})
        self._members: dict[str, str] = {}
        for nc in self.classes.values():
            for net in nc.nets:
                self._members.setdefault(net, nc.name)
        self._resolved: dict[str, NetClassRules] = {}

    @property
    def has_classes(self) -> bool:
        return bool(self.classes)

    def membership(self, net: str | None) -> Membership:
        if net is None or net == "":
            return Membership(
                (DEFAULT_CLASS,),
                RuleSource(RuleSourceKind.NET_CLASS, DEFAULT_CLASS, "no-net items use Default"),
            )
        assigned = self.assignments.get(net)
        if assigned:
            return Membership(
                tuple(assigned),
                RuleSource(RuleSourceKind.NET_CLASS, "+".join(assigned), "netclass assignment"),
            )
        member = self._members.get(net)
        if member is not None:
            return Membership(
                (member,), RuleSource(RuleSourceKind.NET_CLASS, member, "class member list")
            )
        for pat in self.patterns:
            if fnmatchcase(net, pat.pattern):
                return Membership(
                    (pat.netclass,),
                    RuleSource(RuleSourceKind.NET_CLASS, pat.netclass, f"pattern '{pat.pattern}'"),
                )
        return Membership(
            (DEFAULT_CLASS,), RuleSource(RuleSourceKind.NET_CLASS, DEFAULT_CLASS, "default class")
        )

    def rules_for_class(self, name: str) -> NetClassRules:
        cached = self._resolved.get(name)
        if cached is not None:
            return cached
        nc = self.classes.get(name)
        default = self.classes.get(DEFAULT_CLASS)
        values: dict[str, ResolvedValue] = {}
        for field_name in _VALUE_FIELDS:
            own = getattr(nc, field_name) if nc is not None else None
            if own is not None:
                values[field_name] = ResolvedValue(
                    own, RuleSource(RuleSourceKind.NET_CLASS, name, nc.source if nc else None)
                )
                continue
            inherited = getattr(default, field_name) if default is not None else None
            if inherited is not None:
                values[field_name] = ResolvedValue(
                    inherited,
                    RuleSource(
                        RuleSourceKind.NET_CLASS,
                        DEFAULT_CLASS,
                        (
                            f"inherited by '{name}'"
                            if name != DEFAULT_CLASS
                            else default.source if default else None
                        ),
                    ),
                )
            else:
                values[field_name] = unknown(f"net class '{name}' does not state {field_name}")
        rules = NetClassRules(name=name, **values)
        self._resolved[name] = rules
        return rules

    def value_for_net(self, net: str | None, field_name: str) -> ResolvedValue:
        """Class value for ``net``; with several classes the strictest (largest)."""
        membership = self.membership(net)
        best: ResolvedValue | None = None
        for cls in membership.classes:
            v = getattr(self.rules_for_class(cls), field_name)
            if v.value is not None and (best is None or best.value is None or v.value > best.value):
                best = v
        return best if best is not None else unknown(f"no net class states {field_name}")
