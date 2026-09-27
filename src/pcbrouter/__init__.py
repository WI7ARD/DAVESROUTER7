"""AI PCB Router — desktop PCB inspection and AI-assisted autorouting for KiCad boards.

Routing runs in a worker process on a fork, every candidate passes the exact
validator, and exports are gated by checks. The application never modifies the
source board in place.
"""

from __future__ import annotations

__all__ = ["APP_NAME", "APP_SLUG", "STAGE", "__version__"]

#: Human-facing application version (plain PEP 440 since 1.0.0).
__version__ = "1.0.0"

APP_NAME = "AI PCB Router"
#: Filesystem-safe identifier used for config/log directory names.
APP_SLUG = "ai-pcb-router"
STAGE = 10
