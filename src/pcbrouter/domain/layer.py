"""PCB layer model.

Layers are identified by their canonical KiCad *name* (``"F.Cu"``, ``"In1.Cu"``,
``"Edge.Cuts"``), never by ordinal: KiCad renumbered layer ordinals in version 9,
while names have been stable since KiCad 5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

FRONT_COPPER = "F.Cu"
BACK_COPPER = "B.Cu"
EDGE_CUTS = "Edge.Cuts"

_INNER_RE = re.compile(r"^In(\d+)\.Cu$")


class LayerKind(Enum):
    """Physical/semantic classification used by the UI and (later) the router."""

    FRONT_COPPER = "front_copper"
    INNER_COPPER = "inner_copper"
    BACK_COPPER = "back_copper"
    BOARD_OUTLINE = "board_outline"
    TECHNICAL = "technical"  # silk, mask, paste, fab, courtyard
    USER = "user"

    @property
    def is_copper(self) -> bool:
        return self in (LayerKind.FRONT_COPPER, LayerKind.INNER_COPPER, LayerKind.BACK_COPPER)


class CopperLayerType(Enum):
    """KiCad's copper layer usage hint (``signal``/``power``/``mixed``/``jumper``)."""

    SIGNAL = "signal"
    POWER = "power"
    MIXED = "mixed"
    JUMPER = "jumper"
    NOT_COPPER = "user"
    UNKNOWN = "unknown"


def classify_layer_name(name: str) -> LayerKind:
    if name == FRONT_COPPER:
        return LayerKind.FRONT_COPPER
    if name == BACK_COPPER:
        return LayerKind.BACK_COPPER
    if _INNER_RE.match(name):
        return LayerKind.INNER_COPPER
    if name == EDGE_CUTS:
        return LayerKind.BOARD_OUTLINE
    if name.startswith(("F.", "B.")) or name in ("Margin", "Dwgs.User", "Cmts.User"):
        return LayerKind.TECHNICAL
    return LayerKind.USER


def inner_layer_index(name: str) -> int | None:
    """``"In3.Cu" -> 3``; ``None`` for non-inner layers."""
    match = _INNER_RE.match(name)
    return int(match.group(1)) if match else None


def copper_stack_position(name: str) -> int:
    """Physical stacking order for copper layers: F.Cu=0, In1..InN, B.Cu last."""
    if name == FRONT_COPPER:
        return 0
    if name == BACK_COPPER:
        return 10_000
    inner = inner_layer_index(name)
    return inner if inner is not None else 20_000


@dataclass(frozen=True, slots=True)
class Layer:
    """A board layer as declared in the board file's ``(layers ...)`` table."""

    name: str
    kind: LayerKind
    ordinal: int | None = None  # KiCad's numeric id, informational only
    copper_type: CopperLayerType = CopperLayerType.UNKNOWN
    user_name: str | None = None  # optional user-assigned display name

    @property
    def is_copper(self) -> bool:
        return self.kind.is_copper

    @property
    def display_name(self) -> str:
        """User-assigned name with the canonical name, e.g. ``"top_copper (F.Cu)"``."""
        if self.user_name and self.user_name != self.name:
            return f"{self.user_name} ({self.name})"
        return self.name

    @classmethod
    def from_name(cls, name: str, ordinal: int | None = None) -> Layer:
        kind = classify_layer_name(name)
        ctype = CopperLayerType.SIGNAL if kind.is_copper else CopperLayerType.NOT_COPPER
        return cls(name=name, kind=kind, ordinal=ordinal, copper_type=ctype)


def expand_layer_pattern(pattern: str, copper_layers: list[str]) -> list[str]:
    """Expand KiCad wildcard layer names used by pads.

    ``"*.Cu"`` means every copper layer; ``"F&B.Cu"`` means front and back copper.
    Non-wildcard names are returned unchanged.
    """
    if pattern == "*.Cu":
        return list(copper_layers)
    if pattern == "F&B.Cu":
        return [FRONT_COPPER, BACK_COPPER]
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return [f"F{suffix}", f"B{suffix}"]
    return [pattern]
