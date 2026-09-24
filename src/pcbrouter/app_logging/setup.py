"""Logging configuration: console + rotating file, with secret redaction.

Message style: ``event.name key=value key=value`` so logs are grep-able and easy to
parse later without a JSON logging dependency.

SECURITY RULE (effective from Stage 1): API keys and other secrets must never be
logged. Code must not pass secrets to loggers in the first place; the
:class:`SecretRedactingFilter` is a second line of defence that masks anything
that *looks* like a credential before it reaches any handler.
"""

from __future__ import annotations

import logging
import logging.handlers
import platform
import re
import sys
from pathlib import Path
from typing import Final

from pcbrouter import APP_NAME, __version__

ROOT_LOGGER_NAME: Final = "pcbrouter"
LOG_FILENAME: Final = "pcbrouter.log"
MAX_LOG_BYTES: Final = 2 * 1024 * 1024
LOG_BACKUP_COUNT: Final = 5
_FORMAT: Final = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

REDACTED: Final = "[REDACTED]"

# Patterns for common credential shapes. Deliberately broad: a false positive only
# hides part of a log line, a false negative leaks a secret.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI style
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(
        r"(?i)\b((?:api[_-]?key|x-api-key|authorization|secret|token|password)"
        r"\s*[=:]\s*)(['\"]?)[^\s'\",;]{4,}\2"
    ),
)


def redact_secrets(text: str) -> str:
    """Mask substrings that look like credentials."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 1:
            text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Rewrites each record's final message with secrets masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args: let the handler report it
            return True
        redacted = redact_secrets(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if redacted != message or record.args:
            record.msg = redacted
            record.args = None
        return True


def setup_logging(
    log_directory: Path | None,
    level: int = logging.INFO,
    *,
    console: bool = True,
) -> Path | None:
    """Configure the ``pcbrouter`` logger tree. Returns the log file path, if any.

    File logging failure (read-only home, permissions) degrades to console-only
    instead of preventing startup.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)
    redactor = SecretRedactingFilter()

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)

    log_file: Path | None = None
    file_error: str | None = None
    if log_directory is not None:
        try:
            log_directory.mkdir(parents=True, exist_ok=True)
            log_file = log_directory / LOG_FILENAME
            rotating = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
            )
            rotating.setFormatter(formatter)
            rotating.addFilter(redactor)
            logger.addHandler(rotating)
        except OSError as exc:
            file_error = str(exc)
            log_file = None

    if file_error:
        logger.warning("logging.file_unavailable dir=%s error=%s", log_directory, file_error)
    return log_file


def log_startup_banner(logger: logging.Logger | None = None) -> None:
    lg = logger or logging.getLogger(ROOT_LOGGER_NAME)
    lg.info("app.start name=%r version=%s", APP_NAME, __version__)
    lg.info(
        "app.environment os=%r python=%s implementation=%s executable=%s",
        platform.platform(),
        platform.python_version(),
        platform.python_implementation(),
        sys.executable,
    )


def add_handler(handler: logging.Handler) -> None:
    """Attach an extra handler (e.g. the GUI log panel) with redaction applied."""
    handler.addFilter(SecretRedactingFilter())
    logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)


def remove_handler(handler: logging.Handler) -> None:
    logging.getLogger(ROOT_LOGGER_NAME).removeHandler(handler)
