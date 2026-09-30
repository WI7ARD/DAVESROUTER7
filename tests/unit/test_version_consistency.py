"""One version everywhere (1.1.1, P1-4): package, pyproject, router metadata,
changelog, README and the installer build all read or state the same string."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pcbrouter
from pcbrouter.routing import router

ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_pyproject_matches_the_package() -> None:
    m = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M)
    assert m and m.group(1) == pcbrouter.__version__


def test_router_metadata_carries_the_release_version() -> None:
    """Proposals record which router made them; that must be the shipped version,
    not a stale constant."""
    assert pcbrouter.__version__ == router.ROUTER_VERSION


def test_changelog_top_entry_is_this_version() -> None:
    m = re.search(r"^## v(\S+)", _read("CHANGELOG.md"), re.M)
    assert m and m.group(1) == pcbrouter.__version__


def test_readme_states_this_version() -> None:
    assert f"Current version: `{pcbrouter.__version__}`" in _read("README.md")


def test_installer_reads_the_package_version() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_installer_version_check", ROOT / "packaging" / "windows" / "build_installer.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses in the script look themselves up
    spec.loader.exec_module(module)
    assert module.read_version() == pcbrouter.__version__
    assert module.check_version_match() == pcbrouter.__version__


#: interface/format versions of engine components: versioned on their own
#: (bumped when their output format changes), not with the release
COMPONENT_VERSIONS = {"GEOMETRY_ENGINE_VERSION", "RULE_ENGINE_VERSION"}


def test_no_stale_hard_coded_release_versions_in_src() -> None:
    stale = []
    for path in (ROOT / "src" / "pcbrouter").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'(\w*VERSION)\s*=\s*"(\d+\.\d+\.\d+)"', text):
            if m.group(1) not in COMPONENT_VERSIONS and m.group(2) != pcbrouter.__version__:
                stale.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert not stale, stale
