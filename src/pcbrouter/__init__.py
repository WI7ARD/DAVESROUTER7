"""AI PCB Router — desktop PCB inspection and AI-assisted autorouting for KiCad boards.

Stage 9 adds full-board CPU/GPU routing, Freerouting integration, validated
commit/undo and export of a new routed board file. The application never
modifies the source board in place: routing runs in a worker process on a fork,
every candidate passes the exact validator, and exports are gated by checks.
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
