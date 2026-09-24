"""Deterministic design-rule engine (Stage 3). See docs/rules_engine.md."""

from __future__ import annotations

from pcbrouter.rules.model import (
    ItemType,
    ResolvedValue,
    RuleError,
    RuleResolutionError,
    RuleSource,
    RuleSourceKind,
    RuleStatus,
    UnsupportedRule,
    UnsupportedRuleError,
)
from pcbrouter.rules.overrides import BoardOverride, NetOverride, RuleOverrides
from pcbrouter.rules.resolver import RuleResolver
from pcbrouter.rules.ruleset import RULE_ENGINE_VERSION, RuleSet, build_ruleset

__all__ = [
    "RULE_ENGINE_VERSION",
    "BoardOverride",
    "ItemType",
    "NetOverride",
    "ResolvedValue",
    "RuleError",
    "RuleOverrides",
    "RuleResolutionError",
    "RuleResolver",
    "RuleSet",
    "RuleSource",
    "RuleSourceKind",
    "RuleStatus",
    "UnsupportedRule",
    "UnsupportedRuleError",
    "build_ruleset",
]
