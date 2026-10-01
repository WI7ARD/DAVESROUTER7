"""The KiCad compatibility matrix (src/pcbrouter/rules/compat.json) must not claim
more, or less, than the rule engine implements."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pcbrouter.rules.conditions import SUPPORTED_FUNCTIONS, SUPPORTED_PROPERTIES
from pcbrouter.rules.model import SUPPORTED_CONSTRAINTS

MATRIX = Path(__file__).parents[2] / "src" / "pcbrouter" / "rules" / "compat.json"
STATES = {"SUPPORTED", "PARTIAL", "UNSUPPORTED", "UNKNOWN"}


def _rows() -> list[dict]:
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert data["schema"] == "davesrouter-kicad-compat/1"
    return list(data["features"])


def _names(row: dict) -> set[str]:
    # "A.isPlated()" -> isPlated ; "A.Pad_Type / A.Pad_Shape" -> Pad_Type, Pad_Shape
    return set(re.findall(r"([A-Za-z_]+)\(\)|A\.([A-Za-z_]+)", row["feature"])) and {
        a or b for a, b in re.findall(r"([A-Za-z_]+)\(\)|A\.([A-Za-z_]+)", row["feature"])
    }


def test_every_row_has_a_known_state_and_supported_rows_cite_evidence() -> None:
    for row in _rows():
        assert row["status"] in STATES, row
        if row["status"] == "SUPPORTED" and row["kind"] in ("property", "function", "board"):
            assert row.get("evidence"), f"{row['feature']} is SUPPORTED without evidence"


def test_engine_features_are_listed_and_nothing_else_is_claimed() -> None:
    claimed: set[str] = set()
    for row in _rows():
        if row["kind"] in ("property", "function") and row["status"] in ("SUPPORTED", "PARTIAL"):
            claimed |= _names(row)
    engine = set(SUPPORTED_PROPERTIES) | set(SUPPORTED_FUNCTIONS)
    missing = engine - claimed
    assert not missing, f"implemented but not in the matrix: {sorted(missing)}"
    extra = claimed - engine - {"Name"}  # Name: partial three-valued support only
    assert not extra, f"claimed but not implemented: {sorted(extra)}"


def test_constraints_claimed_supported_are_evaluated() -> None:
    for row in _rows():
        if row["kind"] != "constraint" or row["status"] != "SUPPORTED":
            continue
        kinds = {k.strip() for k in row["feature"].split("/")}
        assert kinds <= SUPPORTED_CONSTRAINTS, row["feature"]


def test_rendered_doc_is_up_to_date() -> None:
    import subprocess
    import sys

    tool = MATRIX.parents[3] / "tools" / "compat_matrix.py"
    proc = subprocess.run([sys.executable, str(tool), "--check"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
