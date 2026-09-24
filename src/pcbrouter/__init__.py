"""AI PCB Router — desktop PCB inspection and (future) autorouting for KiCad boards.

Stage 1 is a read-only foundation: it loads ``.kicad_pcb`` files into an internal
domain model and displays them. It performs no autorouting and no AI API calls.
"""

from __future__ import annotations

__all__ = ["APP_NAME", "APP_SLUG", "STAGE", "__version__"]

#: Human-facing application version. Package metadata uses the PEP 440
#: equivalent ``0.1.0+stage1`` (see pyproject.toml).
__version__ = "0.1.0-stage1"

APP_NAME = "AI PCB Router"
#: Filesystem-safe identifier used for config/log directory names.
APP_SLUG = "ai-pcb-router"
STAGE = 1
