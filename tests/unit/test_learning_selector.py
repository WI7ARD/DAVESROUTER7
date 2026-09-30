"""Learning: board profiles, the policy selector (confidence / fallback) and its
router integration. The selector only picks search settings: whatever it
decides, every route is still checked by the exact validator."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.learning.features import DISTANCE_FEATURES, board_profile, transform
from pcbrouter.learning.policy import (
    ArmStats,
    PolicySet,
    ThompsonPolicy,
    attempt_context,
    bucket,
)
from pcbrouter.learning.selector import FALLBACK, LEARNED, SelectivePolicy, Support, fit_support
from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings, make_plan
from pcbrouter.routing.presets import RouteMode, adjust_board_settings
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.working_board import Provenance, WorkingBoard

BOARDS = Path(__file__).parent.parent / "fixtures" / "boards"


def _wb(name: str) -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def _prof(i: float, **over: float) -> dict[str, float]:
    base = {
        "copper_layers": 2.0,
        "signal_layers": 2.0,
        "area_cm2": 20.0 + 4 * i,
        "nets_to_route": 20.0 + 2 * i,
        "pads_per_net": 2.5 + 0.05 * i,
        "pad_density_cm2": 2.0 + 0.1 * i,
        "pitch_p10_mm": 1.0 + 0.05 * i,
        "airwire_mean_mm": 15.0 + i,
        "long_net_frac": 0.2 + 0.01 * i,
        "demand": 0.1 + 0.005 * i,
        "low_escape_frac": 0.1 + 0.01 * i,
        "congestion_mean": 0.6 + 0.005 * i,
        "multi_pad_frac": 0.2,
    }
    return {**base, **over}


def _selective(evidence: dict[int, float] | None, require: bool = True) -> SelectivePolicy:
    sup = fit_support([_prof(i) for i in range(10)], require_evidence=require)
    for i, d in (evidence or {}).items():
        sup.boards[i].evidence["speed"] = {"delta": d, "runs": 2}
    inner = ThompsonPolicy({"c": {"greedier": ArmStats(9, 10)}}, trained_modes=("speed",))
    return SelectivePolicy(inner, sup)


# ------------------------------------------------------------------ features
def test_board_profile_describes_the_job_without_identifying_it() -> None:
    wb = _wb("router_dense.kicad_pcb")
    plan = make_plan(wb, BoardRouterSettings(base_request=RouteRequest("")))
    prof = board_profile(wb.board, plan.tasks)
    assert set(DISTANCE_FEATURES) <= set(prof)
    assert prof["nets_to_route"] == len(plan.tasks) > 0
    assert prof["copper_layers"] >= prof["signal_layers"] >= 1
    assert 0 <= prof["long_net_frac"] <= 1 and prof["pitch_p10_mm"] > 0
    assert all(isinstance(v, float) for v in prof.values())
    assert transform(prof).shape == (len(DISTANCE_FEATURES),)


# ------------------------------------------------------------------ selector
def test_in_distribution_board_with_positive_evidence_uses_the_policy() -> None:
    pol = _selective({4: 3.0, 5: 1.0, 6: 2.0})
    chosen, dec = pol.select(_prof(5))
    assert chosen is pol.inner and dec["decision"] == LEARNED
    assert dec["policy_id"] == pol.inner.policy_id() and 0 <= dec["confidence"] <= 1


def test_out_of_distribution_board_falls_back_and_says_why() -> None:
    pol = _selective({i: 3.0 for i in range(10)})
    chosen, dec = pol.select(_prof(5, signal_layers=6.0, long_net_frac=0.95))
    assert chosen is None and dec["decision"] == FALLBACK
    assert "out of distribution" in dec["reason"] and "signal_layers" in dec["reason"]


def test_no_evidence_or_a_regressing_neighbour_falls_back() -> None:
    chosen, dec = _selective(None).select(_prof(5))
    assert chosen is None and "no board-level A/B evidence" in dec["reason"]
    chosen, dec = _selective({4: 5.0, 5: -1.0, 6: 5.0}).select(_prof(5))
    assert chosen is None and "did not gain" in dec["reason"]
    chosen, _ = _selective(None, require=False).select(_prof(5))
    assert chosen is not None  # evidence is optional only when asked for


def test_support_round_trips_and_refuses_other_feature_sets() -> None:
    sup = fit_support([_prof(i) for i in range(5)])
    again = Support.from_json(sup.to_json())
    assert again.radius == sup.radius and len(again.boards) == 5
    bad = {**sup.to_json(), "features": ["area_cm2"]}
    with pytest.raises(ValueError):
        Support.from_json(bad)


# ------------------------------------------------------------ router wiring
def _route(wb: WorkingBoard, policy: object, mode: RouteMode = RouteMode.SPEED):
    base = RouteRequest("", candidates=1)
    st = adjust_board_settings(BoardRouterSettings(base_request=base, budget_s=120), base, mode)
    return BoardRouter(wb, replace(st, policy=policy)).run()


def test_fallback_routes_exactly_like_the_fixed_router() -> None:
    fixed = _route(_wb("router_dense.kicad_pcb"), None)
    pol = _selective(None)  # no evidence anywhere: always falls back
    fell = _route(_wb("router_dense.kicad_pcb"), pol)
    assert fell.policy_decision is not None and fell.policy_decision["decision"] == FALLBACK
    assert {n: o.status for n, o in fell.outcomes.items()} == {
        n: o.status for n, o in fixed.outcomes.items()
    }
    assert all(not o.arms for o in fell.outcomes.values())
    assert fixed.policy_decision is None


def test_policy_set_picks_the_policy_for_the_job_mode_and_every_route_stays_legal() -> None:
    ctx = attempt_context(bucket("signal", 2, 8, 2), 0)
    greedy = ThompsonPolicy({ctx: {"greedier": ArmStats(9, 10)}}, min_trials=0, margin=0)
    pset = PolicySet({"speed": greedy})
    wb = _wb("router_dense.kicad_pcb")
    res = _route(wb, pset, RouteMode.SPEED)
    assert res.policy_decision is not None and res.policy_decision["decision"] == "POLICY"
    tracks, vias, removed = res.objects_for(None)
    wb.commit_objects(tracks, vias, removed, "policy", Provenance.ROUTER_GENERATED)
    assert not wb.engine.run_drc().errors
    acc = _route(_wb("router_dense.kicad_pcb"), pset, RouteMode.ACCURACY)
    assert acc.policy_decision is not None and acc.policy_decision["decision"] == FALLBACK


def test_every_attempt_is_traced_with_the_settings_it_used() -> None:
    res = _route(_wb("router_basic.kicad_pcb"), None)
    for o in res.outcomes.values():
        assert len(o.trace) == o.attempts
        for t in o.trace:
            assert {"pass", "grid_mm", "heuristic_weight", "coarse_factor", "status"} <= set(t)
            assert t["arm"] is None  # the fixed router uses no arms
