"""Learning: board profiles, the policy selector (confidence / fallback) and its
router integration. The selector only picks search settings: whatever it
decides, every route is still checked by the exact validator."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.learning.features import DISTANCE_FEATURES, board_profile, transform
from pcbrouter.learning.policy import (
    PRESET,
    ArmStats,
    PolicySet,
    ThompsonPolicy,
    attempt_context,
    router_signature,
    task_bucket,
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


def _first_contexts(name: str) -> set[str]:
    """The first-attempt contexts the router will ask about on board *name*."""
    wb = _wb(name)
    plan = make_plan(wb, BoardRouterSettings(base_request=RouteRequest("")))
    prof = board_profile(wb.board, plan.tasks)
    layers, demand = int(prof["signal_layers"]), prof["demand"]
    return {attempt_context(task_bucket(t, layers, demand), 0) for t in plan.tasks}


def test_policy_set_picks_the_policy_for_the_job_mode_and_every_route_stays_legal() -> None:
    strong = {"no_coarse": ArmStats(40, 50), PRESET: ArmStats(10, 50)}  # a Wilson-clear lead
    ctxs = _first_contexts("router_dense.kicad_pcb")
    learned = ThompsonPolicy({c: dict(strong) for c in ctxs})
    pset = PolicySet({"speed": learned})
    wb = _wb("router_dense.kicad_pcb")
    res = _route(wb, pset, RouteMode.SPEED)
    assert res.policy_decision is not None and res.policy_decision["decision"] == "POLICY"
    # the router's contexts (with the board's demand band) match the learned ones
    assert all(o.arms[0] == "no_coarse" for o in res.outcomes.values() if o.arms)
    assert any(o.arms for o in res.outcomes.values())
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


def _outcomes(res: Any) -> dict[str, tuple[Any, float, int]]:
    return {n: (o.status, o.length_nm, o.vias) for n, o in res.outcomes.items()}


def test_policy_from_another_router_falls_back_to_the_fixed_router() -> None:
    strong = {"no_coarse": ArmStats(40, 50), PRESET: ArmStats(10, 50)}
    stats = {c: dict(strong) for c in _first_contexts("router_basic.kicad_pcb")}
    other = {**router_signature(), "app": "0.0.0"}
    stale = ThompsonPolicy(stats, router=other)
    assert stale.compatibility() is not None and "0.0.0" in str(stale.compatibility())
    assert ThompsonPolicy(stats, router=router_signature()).compatibility() is None
    assert ThompsonPolicy(stats).compatibility() is None  # unknown router: old files
    fixed = _route(_wb("router_basic.kicad_pcb"), None)
    for pol in (stale, PolicySet({"speed": stale})):
        fell = _route(_wb("router_basic.kicad_pcb"), pol)
        dec = fell.policy_decision
        assert dec is not None and dec["decision"] == FALLBACK and "retrain" in dec["reason"]
        assert all(not o.arms for o in fell.outcomes.values())
        assert _outcomes(fell) == _outcomes(fixed)


# ------------------------------------------------------------------ app opt-in
def _board_job(policy_path: str | None) -> Any:
    from pcbrouter.jobs.execute import JobContext, run_job
    from pcbrouter.jobs.protocol import RouteBoardJob, WorkingSnapshot

    base = RouteRequest("", candidates=1)
    st = adjust_board_settings(
        BoardRouterSettings(base_request=base, budget_s=60), base, RouteMode.SPEED
    )
    snap = WorkingSnapshot.from_working(_wb("router_basic.kicad_pcb"))
    job = RouteBoardJob(snap, st, policy_path=policy_path)
    return run_job(job, JobContext(1, lambda _msg: None))


def test_route_board_job_uses_the_policy_file_it_is_given(tmp_path: Path) -> None:
    import json

    from pcbrouter.learning.policy import SET_SCHEMA

    strong = {"no_coarse": ArmStats(40, 50), PRESET: ArmStats(10, 50)}
    stats = {c: dict(strong) for c in _first_contexts("router_basic.kicad_pcb")}
    pol = ThompsonPolicy(stats, router=router_signature()).to_json()
    path = tmp_path / "policy_set.json"
    path.write_text(json.dumps({"schema": SET_SCHEMA, "modes": {"speed": pol}}))
    res = _board_job(str(path))
    assert res.policy_decision is not None
    assert res.policy_decision["decision"] == "POLICY"
    assert any(o.arms for o in res.outcomes.values())


def test_route_board_job_with_an_unusable_policy_routes_with_the_fixed_router(
    tmp_path: Path,
) -> None:
    fixed = _board_job(None)
    assert fixed.policy_decision is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    for path in (tmp_path / "missing.json", broken):
        res = _board_job(str(path))  # never raises
        assert res.policy_decision is None
        assert all(not o.arms for o in res.outcomes.values())
        assert _outcomes(res) == _outcomes(fixed)
