"""Partly-supported DRU conditions: three-valued evaluation (Real100 K022).

A rule with an unsupported part (``A.insideArea('Conformal*')``) stays
unsupported, but its supported parts can prove it never applies to a pair:
``A.NetClass == 'HV' && A.insideArea(...)`` cannot match a net outside class HV.
Such pairs no longer get the rule's "possibly stricter" clearance, which
conservative validation enforces everywhere else. Unknown parts are never assumed
false.
"""

from __future__ import annotations

import pytest

from pcbrouter.rules.conditions import ItemFacts, parse_partial
from pcbrouter.rules.model import ItemType

HV = ItemFacts("/HV_OUT", ("HV",), ItemType.TRACK, "F.Cu")
SIG = ItemFacts("/SDA", ("Default",), ItemType.TRACK, "F.Cu")
PAD = ItemFacts("/SDA", ("Default",), ItemType.PAD, "F.Cu")


@pytest.mark.parametrize(
    ("text", "a", "b", "may"),
    [
        ("A.NetClass == 'HV' && A.insideArea('Conformal*')", SIG, SIG, False),
        ("A.NetClass == 'HV' && A.insideArea('Conformal*')", HV, SIG, True),
        # either order: KiCad tests (A, B) and (B, A)
        ("A.NetClass == 'HV' && A.insideArea('Conformal*')", SIG, HV, True),
        ("A.NetClass == 'HV' || A.insideArea('X')", SIG, SIG, True),  # unknown may be true
        ("A.insideCourtyard('U4') || A.insideCourtyard('CW*')", SIG, SIG, True),
        ("!A.insideArea('PadsNearEdge*')", SIG, None, True),
        ("A.Type == 'pad' && !A.insideArea('PadsNearEdge*')", SIG, None, False),
        ("A.Type == 'pad' && !A.insideArea('PadsNearEdge*')", PAD, None, True),
        ("A.Name == 'R1' && A.Type == 'via'", SIG, SIG, False),
        # only zones carry "Name" in KiCad: false for tracks, unknown for zones
        ("A.Name == 'R1'", SIG, SIG, False),
        ("A.Name == 'R1'", ItemFacts("GND", ("Default",), ItemType.ZONE, "F.Cu"), SIG, True),
    ],
)
def test_may_match(text: str, a: ItemFacts, b: ItemFacts | None, may: bool) -> None:
    cond = parse_partial(text)
    assert cond is not None
    assert cond.may_match(a, b) is may


def test_unreadable_syntax_has_no_partial_condition() -> None:
    assert parse_partial("A.Width > 0.2mm") is None


def test_hv_rule_bound_only_reaches_pairs_it_may_apply_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the resolver: the HV-only rule's 0.4 mm bound still applies to an HV
    pair (it might be inside the area) and no longer to anything else."""
    from pathlib import Path

    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import ConstraintSpec, CustomRuleSpec, load_project_rules
    from pcbrouter.routing.working_board import WorkingBoard
    from pcbrouter.rules.ruleset import _compile

    rule = _compile(
        CustomRuleSpec(
            "HvUnderConformal",
            (ConstraintSpec("clearance", min=400_000),),
            "A.NetClass == 'HV' && A.insideArea('Conformal*')",
        ),
        0,
        ("F.Cu", "B.Cu"),
        "test.kicad_dru",
    )
    assert not isinstance(rule, str) and rule.partial_condition is not None  # type: ignore[union-attr]
    path = Path(__file__).parent.parent / "fixtures" / "boards" / "router_basic.kicad_pcb"
    wb = WorkingBoard(load_board(path).board, load_project_rules(path))
    resolver = wb.engine.resolver
    monkeypatch.setattr(resolver.ruleset, "unsupported", (rule,))
    resolver._cache.clear()
    plain = resolver.resolve_clearance("A", "B")
    assert plain.possibly_stricter is None  # no HV net involved: rule cannot apply
    real = resolver.facts
    monkeypatch.setattr(
        resolver,
        "facts",
        lambda net, t=ItemType.TRACK, layer=None: ItemFacts(
            net, ("HV",) if net == "A" else real(net, t, layer).net_classes, t, layer
        ),
    )
    resolver._cache.clear()
    hv = resolver.resolve_clearance("A", "B")
    assert hv.possibly_stricter == 400_000 and "HvUnderConformal" in hv.possibly_stricter_rules


def test_zone_only_property_is_definitely_false_for_tracks_vias_and_pads() -> None:
    # KiCad registers "Name" only on zones; an item without the property yields an
    # undefined value and both == and != against it are false (pcbexpr_evaluator /
    # libeval VALUE::EqualTo / NotEqualTo). Real100 K037: "A.Name == 'outer_pour'".
    from pcbrouter.rules.conditions import ItemFacts, parse_partial
    from pcbrouter.rules.model import ItemType

    track = ItemFacts("/SIG", ("Default",), ItemType.TRACK, "F.Cu")
    pad = ItemFacts("GND", ("Default",), ItemType.PAD, "F.Cu")
    via = ItemFacts("/SIG", ("Default",), ItemType.VIA, "F.Cu")
    zone = ItemFacts("GND", ("Default",), ItemType.ZONE, "F.Cu")
    eq = parse_partial("A.Name == 'outer_pour'")
    ne = parse_partial("A.Name != 'outer_pour'")
    assert eq is not None and ne is not None
    for a, b in ((track, pad), (via, pad), (track, via)):
        assert not eq.may_match(a, b) and not ne.may_match(a, b)
    assert eq.may_match(track, zone) and ne.may_match(zone, pad)  # zone names unknown
    neg = parse_partial("!(A.Name == 'outer_pour')")  # KiCad: !(undefined == x) is true
    assert neg is not None and neg.may_match(track, pad)
    # other uses of the property stay unknown (never guessed)
    rx = parse_partial("A.Name =~ 'out.*'")
    assert rx is not None and rx.may_match(track, pad)


def test_zone_only_rule_no_longer_constrains_track_to_pad_clearance() -> None:
    from pcbrouter.rules.conditions import ItemFacts
    from pcbrouter.rules.model import ItemType

    cond = parse_partial("A.Name == 'outer_pour'")
    assert cond is not None
    t = ItemFacts("/A", ("Default",), ItemType.TRACK, "F.Cu")
    p = ItemFacts("/B", ("Default",), ItemType.PAD, "F.Cu")
    assert not cond.may_match(t, p)
