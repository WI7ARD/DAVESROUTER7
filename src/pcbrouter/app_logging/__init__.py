"""Logging setup.

Named ``app_logging`` rather than ``logging`` (as sketched in the Stage 1 brief) so
that ``pcbrouter.logging`` can never be confused with, or shadow, the standard
library ``logging`` module in tooling, scripts, or relative imports.
"""

from __future__ import annotations

from pcbrouter.app_logging.setup import (
    ROOT_LOGGER_NAME,
    SecretRedactingFilter,
    add_handler,
    log_startup_banner,
    redact_secrets,
    remove_handler,
    setup_logging,
)

__all__ = [
    "ROOT_LOGGER_NAME",
    "SecretRedactingFilter",
    "add_handler",
    "log_startup_banner",
    "redact_secrets",
    "remove_handler",
    "setup_logging",
]
