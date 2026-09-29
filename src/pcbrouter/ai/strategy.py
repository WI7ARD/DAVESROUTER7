"""Versioned planner-prompt strategies (prompt evolution without self-modification).

The built-in instructions in :mod:`pcbrouter.ai.system_prompts` are the default.
A strategy stores per-mode instruction *overrides*; resolving merges them over the
base. Versions are immutable records: tuning creates a child version, activating
an older version is the rollback. Nothing here touches the model or the board.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.system_prompts import MODE_INSTRUCTIONS

MAX_INSTRUCTION_CHARS = 8000
BUILTIN_ID = "builtin"


class PromptStrategy(BaseModel):
    """One named set of planner instruction overrides."""

    model_config = {"frozen": True, "extra": "forbid"}

    strategy_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = Field(default="Untitled strategy", max_length=80)
    note: str = Field(default="", max_length=500)
    parent_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    #: mode value -> replacement instruction text (missing modes use the base)
    mode_instructions: dict[str, str] = Field(default_factory=dict)

    @field_validator("mode_instructions")
    @classmethod
    def _valid_modes(cls, value: dict[str, str]) -> dict[str, str]:
        valid = {m.value for m in AIMode}
        for key, text in value.items():
            if key not in valid:
                raise ValueError(f"unknown planner mode {key!r}")
            if not text.strip():
                raise ValueError(f"empty instruction override for mode {key!r}")
            if len(text) > MAX_INSTRUCTION_CHARS:
                raise ValueError(f"instruction override for mode {key!r} is too long")
        return dict(value)

    @property
    def label(self) -> str:
        when = time.strftime("%Y-%m-%d", time.localtime(self.created_at))
        return f"{self.name} ({when})"


def resolve_instructions(strategy: PromptStrategy | None) -> dict[AIMode, str]:
    """Base instructions with the strategy's overrides applied (a copy)."""
    resolved = dict(MODE_INSTRUCTIONS)
    if strategy is not None:
        for key, text in strategy.mode_instructions.items():
            resolved[AIMode(key)] = text
    return resolved


#: Safety invariants every instruction override must restate. Overrides
#: *replace* the base text for their mode, so an override that drops these
#: silently un-guards the planner (prose instead of JSON, unapproved actions).
_SAFETY_MARKERS = (
    ("json", "must require exactly-one-JSON-object output"),
    ("approv", "must require user approval before anything runs"),
    ("validat", "must require local deterministic validation"),
)


def safety_issues(text: str) -> list[str]:
    """Missing safety invariants in an instruction override (empty = safe)."""
    lowered = text.lower()
    return [why for marker, why in _SAFETY_MARKERS if marker not in lowered]


def snapshot_effective(
    name: str, note: str, base: PromptStrategy | None, resolved: dict[AIMode, str]
) -> PromptStrategy:
    """Freeze currently-effective instructions as a new self-contained version."""
    return PromptStrategy(
        name=name,
        note=note,
        parent_id=base.strategy_id if base is not None else BUILTIN_ID,
        mode_instructions={m.value: text for m, text in resolved.items()},
    )


def active_strategy(
    strategies: list[PromptStrategy], active_id: str | None
) -> PromptStrategy | None:
    """The selected version, or None for the built-in default."""
    if not active_id or active_id == BUILTIN_ID:
        return None
    return next((s for s in strategies if s.strategy_id == active_id), None)


def describe_diff(old: dict[AIMode, str], new: dict[AIMode, str]) -> dict[str, Any]:
    """Per-mode character deltas between two resolved instruction sets."""
    out: dict[str, Any] = {}
    for mode in AIMode:
        a, b = old.get(mode, ""), new.get(mode, "")
        if a != b:
            out[mode.value] = {"before_chars": len(a), "after_chars": len(b)}
    return out
