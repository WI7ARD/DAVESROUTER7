"""Deterministic rule checks for what the *user asked* (Stage 3, spec §89).

Example: "Can I route CAN at 0.15 mm?" — whatever the model answers, the application
shows its own verdict next to it::

    DETERMINISTIC RULE CHECK  /CAN_H width 0.15 mm: INVALID — minimum 0.2 mm
                              (Custom rule "CAN width")

Nets are matched by exact name or by prefix at a ``_`` boundary ("CAN" -> "CAN_H",
"/CAN_L"); lengths are read as ``<number> mm`` / ``<number> mil``. A length right
after "clearance", "spacing" or "gap" is checked as a clearance, otherwise as a
track width when the prompt is about routing. This only *reads* the prompt locally;
nothing is sent anywhere.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pcbrouter.ai.command_validator import RuleCheck
from pcbrouter.domain.units import format_mm, mil_to_internal, mm_to_internal

if TYPE_CHECKING:
    from pcbrouter.board_engine import BoardEngine

_LENGTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|mil|mils)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z0-9_/+.-]{2,}")
_CLEARANCE_WORDS = ("clearance", "spacing", "gap", "space")
_ROUTING_WORDS = ("route", "routing", "width", "wide", "trace", "track", "thick")
MAX_NETS = 6


def matched_nets(prompt: str, nets: list[str]) -> list[str]:
    words = {w.strip("?.,;:!").upper() for w in _WORD_RE.findall(prompt)}
    found: list[str] = []
    for net in nets:
        bare = net.lstrip("/").upper()
        if not bare:
            continue
        if any(bare == w or bare.startswith(w + "_") for w in words if len(w) >= 2):
            found.append(net)
    return found[:MAX_NETS]


def _lengths(prompt: str) -> list[tuple[int, bool]]:
    """(value nm, is_clearance) for each length in the prompt."""
    out: list[tuple[int, bool]] = []
    lowered = prompt.lower()
    for m in _LENGTH_RE.finditer(prompt):
        number, unit = m.group(1), m.group(2).lower()
        value = mm_to_internal(float(number)) if unit == "mm" else mil_to_internal(float(number))
        before = lowered[max(0, m.start() - 30) : m.start()]
        out.append((value, any(w in before for w in _CLEARANCE_WORDS)))
    return out


def prompt_rule_checks(prompt: str, engine: BoardEngine | None) -> list[RuleCheck]:
    if engine is None or not prompt.strip():
        return []
    resolver = engine.resolver
    nets = matched_nets(prompt, [n.name for n in engine.board.nets if n.name])
    if not nets:
        return []
    lengths = _lengths(prompt)
    about_routing = any(w in prompt.lower() for w in _ROUTING_WORDS)
    checks: list[RuleCheck] = []
    for net in nets:
        width_rules = resolver.width_rules(net)
        clearance = resolver.resolve_net_clearance(net)
        tested = False
        for value, is_clearance in lengths:
            if is_clearance:
                tested = True
                label = f"{net} clearance {format_mm(value)}"
                if clearance.value is None:
                    checks.append(RuleCheck(label, "RULE_UNKNOWN", "no clearance rule applies"))
                elif value < clearance.value:
                    checks.append(
                        RuleCheck(
                            label,
                            "INVALID",
                            f"minimum clearance {format_mm(clearance.value)}",
                            clearance.source.describe(),
                        )
                    )
                else:
                    checks.append(
                        RuleCheck(
                            label,
                            "VALID",
                            f"meets the {format_mm(clearance.value)} clearance",
                            clearance.source.describe(),
                        )
                    )
            elif about_routing:
                tested = True
                minimum = width_rules.minimum
                label = f"{net} width {format_mm(value)}"
                if minimum.value is None:
                    checks.append(
                        RuleCheck(label, "RULE_UNKNOWN", "no rule states a minimum width")
                    )
                elif value < minimum.value:
                    checks.append(
                        RuleCheck(
                            label,
                            "INVALID",
                            f"minimum width {format_mm(minimum.value)}",
                            minimum.source.describe(),
                        )
                    )
                elif width_rules.maximum.value is not None and value > width_rules.maximum.value:
                    checks.append(
                        RuleCheck(
                            label,
                            "INVALID",
                            f"maximum width {format_mm(width_rules.maximum.value)}",
                            width_rules.maximum.source.describe(),
                        )
                    )
                else:
                    checks.append(
                        RuleCheck(
                            label,
                            "VALID",
                            f"meets the {format_mm(minimum.value)} minimum",
                            minimum.source.describe(),
                        )
                    )
        if not tested:
            pref, minimum = width_rules.preferred, width_rules.minimum
            detail = (
                f"preferred width {format_mm(pref.value) if pref.value else 'unknown'}, "
                f"minimum {format_mm(minimum.value) if minimum.value else 'unknown'}, "
                f"clearance {format_mm(clearance.value) if clearance.value else 'unknown'}"
            )
            checks.append(RuleCheck(f"{net} rules", "FACT", detail, minimum.source.describe()))
    return checks
