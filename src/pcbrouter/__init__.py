"""AI PCB Router — desktop PCB inspection and (future) autorouting for KiCad boards.

Stage 2 added an AI engineering layer: natural-language requests are compiled into
validated, previewable PCB commands. Stage 3 adds the deterministic geometry and
design-rule engine (spatial index, rule resolution, collision checks, connectivity,
internal geometry DRC, routing occupancy) that the Stage 4 router will call. The
application still performs no autorouting and never modifies board geometry or
files; the geometry/rule engine, not the AI, decides what is legal.
"""

from __future__ import annotations

__all__ = ["APP_NAME", "APP_SLUG", "STAGE", "__version__"]

#: Human-facing application version. Package metadata uses the PEP 440
#: equivalent ``0.9.0+stage9`` (see pyproject.toml).
__version__ = "0.9.0-stage9"

APP_NAME = "AI PCB Router"
#: Filesystem-safe identifier used for config/log directory names.
APP_SLUG = "ai-pcb-router"
STAGE = 9
