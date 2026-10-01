"""Rule fidelity: our rule engine must answer custom-rule conditions exactly as
KiCad does.

Each case is a tiny board (tests/support/kicadgen.py) whose only possible
violation is a custom clearance rule: the pair violates it iff the rule's
condition matches. ``kicad`` is KiCad's verdict, recorded with KiCad 8.0.8
DRC (the golden). The engine must agree with it always; when kicad-cli is
installed the golden itself is re-checked against live KiCad.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pcbrouter.kicad import oracle
from pcbrouter.rules.conditions import wild_compare
from tests.support import kicadgen as gen

GOLDEN_KICAD = "8.0.8"

# (id, condition, board options, KiCad 8.0.8 verdict)
CASES: list[tuple[str, str | None, dict[str, Any], bool]] = [
    ("no_rule", None, {}, False),
    ("netname_exact", "A.NetName == '/SIG'", {}, True),
    ("netname_case_insensitive", "A.NetName == '/sig'", {}, True),
    ("netname_wildcard_right", "A.NetName == '/S*'", {}, True),
    ("netname_wildcard_left_is_literal", "'/S*' == A.NetName", {}, False),
    ("bus_brackets_are_literal", "A.NetName == '/D[0]'", {"net_a": "/D[0]"}, True),
    ("netclass_case_insensitive", "A.NetClass == 'hv'", {"classes": {"HV": ["/SIG"]}}, True),
    ("type_case_insensitive", "A.Type == 'TRACK' && B.Type == 'Track'", {}, True),
]


@pytest.mark.parametrize(
    ("cond", "opts", "kicad"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_engine_agrees_with_kicad(
    tmp_path: Path, cond: str | None, opts: dict[str, Any], kicad: bool
) -> None:
    board = gen.board(tmp_path, dru=gen.clearance_rule(cond) if cond else None, **opts)
    assert gen.ours_flags(board) is kicad


_TOOL = oracle.find_oracle()


@pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")
@pytest.mark.parametrize(
    ("cond", "opts", "kicad"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_golden_still_matches_live_kicad(
    tmp_path: Path, cond: str | None, opts: dict[str, Any], kicad: bool
) -> None:
    board = gen.board(tmp_path, dru=gen.clearance_rule(cond) if cond else None, **opts)
    assert gen.kicad_flags(_TOOL, board) is kicad, f"KiCad {_TOOL.version if _TOOL else '?'}"


def test_wild_compare_ports_kicad_wildcard_string_compare() -> None:
    assert wild_compare("/S*", "/sig") and wild_compare("*", "")
    assert wild_compare("A?C", "abc") and not wild_compare("A?C", "abbc")
    assert wild_compare("/D[0]", "/d[0]") and not wild_compare("/D[0]", "/D0")
    assert wild_compare("*_P", "USB_P") and not wild_compare("*_P", "USB_PX")
    assert wild_compare("a*b*c", "aXXbYYc") and not wild_compare("a*b*c", "aXXbYY")
    assert not wild_compare("hv", "HV", case_sensitive=True)  # wxString::Matches
