"""AI PCB Router — desktop PCB inspection and (future) autorouting for KiCad boards.

Stage 2 adds an AI engineering layer: natural-language requests are compiled into
validated, previewable PCB commands. It still performs no autorouting and never
modifies board geometry or files. AI requests are only sent when the user explicitly
asks, to a provider the user configured.
"""

from __future__ import annotations

__all__ = ["APP_NAME", "APP_SLUG", "STAGE", "__version__"]

#: Human-facing application version. Package metadata uses the PEP 440
#: equivalent ``0.2.0+stage2`` (see pyproject.toml).
__version__ = "0.2.0-stage2"

APP_NAME = "AI PCB Router"
#: Filesystem-safe identifier used for config/log directory names.
APP_SLUG = "ai-pcb-router"
STAGE = 2
