from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest

from pcbrouter import __version__
from pcbrouter.app_logging import (
    ROOT_LOGGER_NAME,
    log_startup_banner,
    redact_secrets,
    setup_logging,
)


@pytest.fixture
def clean_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    yield logger
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx1234",
        "sk-AbCdEfGhIjKlMnOpQrStUv",
        "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def",
    ],
)
def test_redacts_token_shapes(secret: str) -> None:
    out = redact_secrets(f"calling provider with {secret} now")
    assert secret not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize(
    "text",
    ["api_key=abcd1234efgh", "API-KEY: 'hunter2hunter2'", "password = s3cr3t!!", "token:xyzw9876"],
)
def test_redacts_key_value_pairs(text: str) -> None:
    value = text.split("=")[-1].split(":")[-1].strip(" '")
    out = redact_secrets(text)
    assert value not in out
    assert "[REDACTED]" in out


def test_ordinary_engineering_text_is_untouched() -> None:
    text = "board.load.done path=/tmp/x.kicad_pcb tracks=12 net=/SIG task-12 disk-usage"
    assert redact_secrets(text) == text


def test_file_and_console_handlers_with_redaction(
    tmp_path: Path, clean_logger: logging.Logger
) -> None:
    log_file = setup_logging(tmp_path / "logs", logging.DEBUG, console=True)
    assert log_file is not None
    kinds = {type(h) for h in clean_logger.handlers}
    assert logging.handlers.RotatingFileHandler in kinds
    assert logging.StreamHandler in kinds
    log_startup_banner(clean_logger)
    clean_logger.info("provider key=%s", "sk-ant-api03-SUPERSECRETVALUE123")
    for h in clean_logger.handlers:
        h.flush()
    content = log_file.read_text(encoding="utf-8")
    assert __version__ in content
    assert "python=" in content and "os=" in content
    assert "SUPERSECRETVALUE" not in content


def test_unwritable_log_dir_degrades_to_console(
    tmp_path: Path, clean_logger: logging.Logger
) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert setup_logging(blocker / "logs", console=True) is None
    assert any(isinstance(h, logging.StreamHandler) for h in clean_logger.handlers)
