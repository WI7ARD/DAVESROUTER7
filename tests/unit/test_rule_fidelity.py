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

# (id, condition, board options, KiCad 8.0.8 verdict[, our verdict])
# Our verdict defaults to "flag" / "clear" (exact agreement). "bound" means the
# engine cannot evaluate a fact at this call site (pad type, hole plating) and
# routes with the stricter value instead: safe, possibly over-conservative.
CASES: list[tuple[Any, ...]] = [
    ("no_rule", None, {}, False),
    ("netname_exact", "A.NetName == '/SIG'", {}, True),
    ("netname_case_insensitive", "A.NetName == '/sig'", {}, True),
    ("netname_wildcard_right", "A.NetName == '/S*'", {}, True),
    ("netname_wildcard_left_is_literal", "'/S*' == A.NetName", {}, False),
    ("bus_brackets_are_literal", "A.NetName == '/D[0]'", {"net_a": "/D[0]"}, True),
    ("netclass_case_insensitive", "A.NetClass == 'hv'", {"classes": {"HV": ["/SIG"]}}, True),
    ("type_case_insensitive", "A.Type == 'TRACK' && B.Type == 'Track'", {}, True),
    # copper text is copper: tracks keep their clearance from it (K067 'MOD0')
    ("copper_text_too_close", None, {"item": gen.Item("text"), "gap_mm": 0.02}, True),
    ("copper_text_clear", None, {"item": gen.Item("text"), "gap_mm": 2.0}, False),
    ("text_on_other_layer", None, {"item": gen.Item("text", "B.Cu"), "gap_mm": 0.02}, False),
    # '=~' is not KiCad syntax: KiCad 8/9 skip the whole rule, so must we
    ("regex_operator_rule_is_skipped", "A.NetName =~ '/S.*'", {}, False),
    # A.Net / B.Net: the net code
    ("net_differs", "A.Net != B.Net", {}, True),
    ("net_same_is_false", "A.Net == B.Net", {}, False),
    # isPlated(): PTH pads and vias
    ("via_is_plated", "A.isPlated()", {"item": gen.Item("via")}, True),
    ("track_is_not_plated", "A.isPlated() && B.isPlated()", {"item": gen.Item("via")}, False),
    ("smd_pad_plating_unknown", "A.isPlated()", {"item": gen.Item("smd")}, False, "bound"),
    # existsOnLayer(): wildcard over layer names, against the item's layer set
    ("exists_on_front", "A.existsOnLayer('F.Cu') && B.existsOnLayer('F.Cu')", {}, True),
    ("exists_on_back_is_false", "A.existsOnLayer('B.*')", {}, False),
    ("exists_on_any_copper", "A.existsOnLayer('*.Cu')", {}, True),
    # inDiffPair(): P/N or +/- suffix with an existing partner net
    (
        "diff_pair_member",
        "A.inDiffPair('USB')",
        {"net_a": "USBP", "extra_nets": ("USBN",)},
        True,
    ),
    (
        "diff_pair_base_before_underscore",
        "A.inDiffPair('CLK')",
        {"net_a": "CLK_P", "extra_nets": ("CLK_N",)},
        True,
    ),
    ("diff_pair_needs_partner", "A.inDiffPair('*')", {"net_a": "USBP"}, False),
    ("not_a_diff_pair", "A.inDiffPair('*')", {}, False),
    # Pad_Type: pad-only; undefined (so false) on tracks
    ("pad_type_on_tracks_is_false", "A.Pad_Type == 'SMD' || B.Pad_Type == 'SMD'", {}, False),
    ("pad_type_smd", "A.Pad_Type == 'SMD'", {"item": gen.Item("smd")}, True, "bound"),
    ("pad_type_other", "A.Pad_Type == 'Through-hole'", {"item": gen.Item("smd")}, False, "bound"),
]


def _ours(case: tuple[Any, ...]) -> str:
    return str(case[4]) if len(case) > 4 else ("flag" if case[3] else "clear")


@pytest.mark.parametrize(
    ("cond", "opts", "kicad", "ours"),
    [(*c[1:4], _ours(c)) for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_engine_agrees_with_kicad(
    tmp_path: Path, cond: str | None, opts: dict[str, Any], kicad: bool, ours: str
) -> None:
    board = gen.board(tmp_path, dru=gen.clearance_rule(cond) if cond else None, **opts)
    verdict = gen.ours_verdict(board)
    assert verdict == ours
    assert not (kicad and verdict == "clear"), "KiCad flags what the engine calls clean"


_TOOL = oracle.find_oracle()


@pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")
@pytest.mark.parametrize(
    ("cond", "opts", "kicad"), [c[1:4] for c in CASES], ids=[c[0] for c in CASES]
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
