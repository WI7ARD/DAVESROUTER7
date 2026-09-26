"""Session/project router overrides — distinct from the source KiCad rules.

Two origins, both explicit user actions:

* manual overrides (Tools ▸ Routing Rule Overrides);
* constraints the user approved from the AI planner (Stage 2 proposals).

Safety (see docs/rules_engine.md): an override may *tighten* a known rule or
*define* a value that no rule states; it can never weaken a known rule. A width
override below the resolved minimum is not applied — validation reports it.
Overrides are stored in the board's workspace (never in the KiCad files).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from pcbrouter.domain.units import Nm
from pcbrouter.rules.model import RuleSource, RuleSourceKind

log = logging.getLogger(__name__)

OVERRIDES_FILENAME = "router_overrides.json"
OVERRIDES_VERSION = 1


@dataclass(frozen=True, slots=True)
class NetOverride:
    width: Nm | None = None
    clearance: Nm | None = None
    max_vias: int | None = None
    allowed_layers: tuple[str, ...] | None = None
    forbidden_layers: tuple[str, ...] = ()
    origin: str = "user"  # "user" or an AI proposal id

    def source(self) -> RuleSource:
        if self.origin == "user":
            return RuleSource(RuleSourceKind.USER_OVERRIDE)
        return RuleSource(RuleSourceKind.AI_CONSTRAINT, self.origin)

    def merged(self, other: NetOverride) -> NetOverride:
        """``other`` wins where it states a value."""
        return NetOverride(
            width=other.width if other.width is not None else self.width,
            clearance=other.clearance if other.clearance is not None else self.clearance,
            max_vias=other.max_vias if other.max_vias is not None else self.max_vias,
            allowed_layers=(
                other.allowed_layers if other.allowed_layers is not None else self.allowed_layers
            ),
            forbidden_layers=tuple(dict.fromkeys(self.forbidden_layers + other.forbidden_layers)),
            origin=other.origin,
        )


@dataclass(frozen=True, slots=True)
class BoardOverride:
    clearance: Nm | None = None  # default copper clearance where no rule states one
    edge_clearance: Nm | None = None
    track_width: Nm | None = None  # default preferred width where no rule states one
    max_vias: int | None = None


@dataclass(frozen=True, slots=True)
class RuleOverrides:
    board: BoardOverride = field(default_factory=BoardOverride)
    nets: dict[str, NetOverride] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.board == BoardOverride() and not self.nets

    def for_net(self, net: str | None) -> NetOverride | None:
        return self.nets.get(net) if net else None

    def with_net(self, net: str, override: NetOverride) -> RuleOverrides:
        nets = dict(self.nets)
        nets[net] = nets[net].merged(override) if net in nets else override
        return replace(self, nets=nets)

    def combined(self, other: RuleOverrides) -> RuleOverrides:
        """``other`` (e.g. AI-approved constraints) layered over ``self`` (manual)."""
        result = self
        for net, ov in other.nets.items():
            result = result.with_net(net, ov)
        b = other.board
        mine = self.board
        board = BoardOverride(
            clearance=b.clearance if b.clearance is not None else mine.clearance,
            edge_clearance=(
                b.edge_clearance if b.edge_clearance is not None else mine.edge_clearance
            ),
            track_width=b.track_width if b.track_width is not None else mine.track_width,
            max_vias=b.max_vias if b.max_vias is not None else mine.max_vias,
        )
        return replace(result, board=board)

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": OVERRIDES_VERSION,
            "board": asdict(self.board),
            "nets": {
                net: {
                    **asdict(ov),
                    "allowed_layers": (
                        list(ov.allowed_layers) if ov.allowed_layers is not None else None
                    ),
                    "forbidden_layers": list(ov.forbidden_layers),
                }
                for net, ov in sorted(self.nets.items())
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleOverrides:
        board_raw = data.get("board") or {}
        board = BoardOverride(
            **{
                k: board_raw.get(k)
                for k in ("clearance", "edge_clearance", "track_width", "max_vias")
            }
        )
        nets: dict[str, NetOverride] = {}
        for net, raw in (data.get("nets") or {}).items():
            allowed = raw.get("allowed_layers")
            nets[str(net)] = NetOverride(
                width=raw.get("width"),
                clearance=raw.get("clearance"),
                max_vias=raw.get("max_vias"),
                allowed_layers=tuple(allowed) if allowed is not None else None,
                forbidden_layers=tuple(raw.get("forbidden_layers") or ()),
                origin=str(raw.get("origin") or "user"),
            )
        return cls(board=board, nets=nets)


def load_overrides(workspace_root: Path) -> RuleOverrides:
    path = workspace_root / OVERRIDES_FILENAME
    if not path.is_file():
        return RuleOverrides()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RuleOverrides.from_dict(data if isinstance(data, dict) else {})
    except (OSError, ValueError, TypeError) as exc:
        log.warning("rules.overrides.load_failed path=%s error=%s", path, exc)
        return RuleOverrides()


def save_overrides(workspace_root: Path, overrides: RuleOverrides) -> Path:
    """Atomic write into the workspace (never next to the KiCad project)."""
    workspace_root.mkdir(parents=True, exist_ok=True)
    path = workspace_root / OVERRIDES_FILENAME
    fd, tmp = tempfile.mkstemp(prefix=".overrides-", suffix=".tmp", dir=workspace_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(overrides.to_dict(), fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    log.info("rules.overrides.saved path=%s nets=%d", path, len(overrides.nets))
    return path
