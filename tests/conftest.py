"""Shared test configuration.

* Qt runs headless (offscreen) so the suite works in CI and over SSH.
* Every test gets private config/data/log directories: tests never read or write
  the real user's settings, and never depend on private PCB projects.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pcbrouter.kicad import LoadResult, load_board

FIXTURES = Path(__file__).parent / "fixtures" / "boards"


@pytest.fixture(autouse=True)
def isolated_app_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "appdirs"
    for var, sub in (
        ("PCBROUTER_CONFIG_DIR", "config"),
        ("PCBROUTER_DATA_DIR", "data"),
        ("PCBROUTER_LOG_DIR", "logs"),
    ):
        monkeypatch.setenv(var, str(base / sub))
    return base


@pytest.fixture
def fixture_path() -> Callable[[str], Path]:
    def get(name: str) -> Path:
        path = FIXTURES / name
        assert path.exists(), f"missing fixture {path}"
        return path

    return get


@pytest.fixture
def load_fixture(fixture_path: Callable[[str], Path]) -> Callable[[str], LoadResult]:
    def load(name: str) -> LoadResult:
        return load_board(fixture_path(name))

    return load


@pytest.fixture(autouse=True)
def reset_app_logger() -> Iterator[None]:
    """``setup_logging`` configures a process-global logger; undo it after each test so
    handlers bound to pytest's (closed) capture streams never leak between tests."""
    yield
    import logging

    logger = logging.getLogger("pcbrouter")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


@pytest.fixture
def can_board(load_fixture: Callable[[str], LoadResult]):  # type: ignore[no-untyped-def]
    """Board with CAN_H, CAN_L, VBAT, GND, UART_TX, a locked U2 and an adversarial U99."""
    return load_fixture("can_node.kicad_pcb").board


# Shared fixtures from test support modules.
from tests.support.keyrings import memory_keyring, no_secure_keyring  # noqa: E402, F401


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    """Hardware gate for GPU tests: probe once; without a device every ``gpu`` test
    is reported SKIPPED (with the probe's reason), never failed."""
    gpu_items = [item for item in items if item.get_closest_marker("gpu") is not None]
    if not gpu_items:
        return
    from pcbrouter.compute.probe import gpu_gate

    gate = gpu_gate("pytest-gpu-tests")
    if gate.available:
        return
    marker = pytest.mark.skip(reason=f"GPU SKIPPED: {gate.reason}")
    for item in gpu_items:
        item.add_marker(marker)
