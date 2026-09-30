"""Learning level 2: search-variant policies (no router needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.learning.policy import (
    ARMS,
    DEMAND_EDGES,
    MAX_HEURISTIC_WEIGHT,
    PRESET,
    ArmStats,
    Attempt,
    Policy,
    RandomPolicy,
    ThompsonPolicy,
    attempt_context,
    attempts,
    bucket,
    learn,
    policy_from_spec,
    retry_kind,
)
from pcbrouter.routing.request import RouteRequest


def test_every_arm_only_changes_search_settings() -> None:
    base = RouteRequest("N", preferred_width=250_000, max_vias=3, forbidden_layers=("B.Cu",))
    for name, fn in ARMS.items():
        r = fn(base)
        # rule-facing fields are never touched by an arm
        assert (r.net, r.preferred_width, r.max_vias, r.forbidden_layers) == (
            base.net, base.preferred_width, base.max_vias, base.forbidden_layers
        ), name  # fmt: skip
        assert r.grid_resolution >= 50_000 and r.heuristic_weight >= 1.0


def test_random_policy_is_reproducible_and_covers_arms() -> None:
    p, q = RandomPolicy(7), RandomPolicy(7)
    picks = [p.choose("c", f"n{i}", 0) for i in range(200)]
    assert picks == [q.choose("c", f"n{i}", 0) for i in range(200)]
    assert set(picks) == set(ARMS)


def _rec(arms: list[str], status: str, kind: str = "signal") -> dict:
    return {
        "net_f": {"kind": kind, "pads": 2, "escape_options": 8},
        "board_f": {"copper_layers": 2},
        "outcome": {"arms": arms, "status": status},
    }


def test_learn_counts_attempts_and_credits_only_the_last() -> None:
    pol = learn([_rec([PRESET, "no_coarse"], "SUCCESS"), _rec([PRESET], "NO_ROUTE")])
    base = bucket("signal", 2, 8, 2)
    first, retry = pol.stats[attempt_context(base, 0)], pol.stats[attempt_context(base, 1)]
    assert (first[PRESET].wins, first[PRESET].trials) == (0, 2)
    assert (retry["no_coarse"].wins, retry["no_coarse"].trials) == (1, 1)


def test_records_without_arms_teach_nothing() -> None:
    assert learn([{"outcome": {"status": "SUCCESS"}, "net_f": {}}]).stats == {}


def test_frozen_policy_needs_evidence_and_a_margin() -> None:
    ctx = "c"
    weak = ThompsonPolicy({ctx: {PRESET: ArmStats(5, 10), "greedier": ArmStats(4, 5)}})
    assert weak.choose(ctx, "n", 0) == PRESET  # 5 trials < min_trials
    close = ThompsonPolicy({ctx: {PRESET: ArmStats(50, 100), "greedier": ArmStats(52, 100)}})
    assert close.choose(ctx, "n", 0) == PRESET  # intervals overlap
    strong = ThompsonPolicy({ctx: {PRESET: ArmStats(30, 100), "greedier": ArmStats(80, 100)}})
    assert strong.choose(ctx, "n", 0) == "greedier"
    assert ThompsonPolicy().choose("unseen", "n", 0) == PRESET


def test_policy_round_trips_and_specs(tmp_path: Path) -> None:
    pol = ThompsonPolicy({"c": {PRESET: ArmStats(1, 2)}})
    path = tmp_path / "policy.json"
    pol.save(path)
    again = ThompsonPolicy.load(path)
    assert again.stats["c"][PRESET].trials == 2
    assert policy_from_spec(None) is None and policy_from_spec("fixed") is None
    assert isinstance(policy_from_spec("random:3"), RandomPolicy)
    assert isinstance(policy_from_spec(str(path)), ThompsonPolicy)
    with pytest.raises(ValueError):
        ThompsonPolicy.from_json({"schema": "other"})


def test_router_records_one_arm_per_attempt_and_stays_legal() -> None:
    from dataclasses import replace

    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.learning.experience import records_from_result
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.working_board import Provenance, WorkingBoard

    path = Path(__file__).parent.parent / "fixtures" / "boards" / "router_dense.kicad_pcb"
    base = BoardRouterSettings(base_request=RouteRequest("", candidates=1), budget_s=120)
    for policy in (None, RandomPolicy(3)):
        wb = WorkingBoard(load_board(path).board, load_project_rules(path))
        res = BoardRouter(wb, replace(base, policy=policy)).run()
        for o in res.outcomes.values():
            assert len(o.arms) == (o.attempts if policy is not None else 0)
            assert all(a in ARMS for a in o.arms)
        tracks, vias, removed = res.objects_for(None)
        wb.commit_objects(tracks, vias, removed, "policy", Provenance.ROUTER_GENERATED)
        assert not wb.engine.run_drc().errors  # every arm's copper is validator-clean
        recs = records_from_result(res, replace(base, policy=policy), salt="s")
        assert all(r["outcome"]["policy"] == ("random" if policy else "fixed") for r in recs)
        assert all(r["net_f"]["bucket"] for r in recs)


def test_cli_route_loads_a_trained_policy_and_rejects_a_bad_one(tmp_path: Path) -> None:
    import json

    from pcbrouter.app.route_cli import route_cli

    src = Path(__file__).parent.parent / "fixtures" / "boards" / "router_basic.kicad_pcb"
    for suffix in (".kicad_pcb", ".kicad_pro"):
        s = src.with_suffix(suffix)
        if s.exists():
            (tmp_path / s.name).write_bytes(s.read_bytes())
    board = tmp_path / src.name
    good = tmp_path / "policy.json"
    ThompsonPolicy({"c": {"greedier": ArmStats(9, 10)}}).save(good)
    report = tmp_path / "r.json"
    code = route_cli(board, tmp_path / "o.kicad_pcb", "speed", 0, 60.0, report, False,
                     policy=str(good))  # fmt: skip
    assert code in (0, 3) and json.loads(report.read_text())["policy"] == "thompson"
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": "something-else"}')
    code = route_cli(board, tmp_path / "o2.kicad_pcb", "speed", 0, 60.0, report, False,
                     policy=str(bad))  # fmt: skip
    assert code not in (0, 3) and not (tmp_path / "o2.kicad_pcb").exists()


def test_policy_remembers_the_modes_it_was_trained_for() -> None:
    data = ThompsonPolicy().to_json()
    data["trained"] = {"config": {"modes": ["speed"]}}
    assert ThompsonPolicy.from_json(data).trained_modes == ("speed",)
    assert ThompsonPolicy.from_json(ThompsonPolicy().to_json()).trained_modes is None


# ------------------------------------------------------------------ v3 policy rules
def test_greedier_is_capped_at_the_speed_presets_bound() -> None:
    greedier = ARMS["greedier"]
    assert greedier(RouteRequest("N", heuristic_weight=1.0)).heuristic_weight == 1.25  # Accuracy
    assert greedier(RouteRequest("N", heuristic_weight=1.3)).heuristic_weight == 1.5
    speed = RouteRequest("N", heuristic_weight=MAX_HEURISTIC_WEIGHT)
    assert greedier(speed) == speed  # Speed is already at 1.5: a no-op


class _AlwaysGreedier(Policy):
    name = "always-greedier"

    def choose(self, ctx: str, net: str, attempt: int) -> str:
        return "greedier"


def test_router_records_an_arm_that_changes_nothing_as_the_preset() -> None:
    from dataclasses import replace

    from pcbrouter.kicad.loader import load_board
    from pcbrouter.kicad.rule_adapter import load_project_rules
    from pcbrouter.routing.board_router import BoardRouter, BoardRouterSettings
    from pcbrouter.routing.presets import RouteMode, adjust_board_settings
    from pcbrouter.routing.working_board import WorkingBoard

    path = Path(__file__).parent.parent / "fixtures" / "boards" / "router_basic.kicad_pcb"
    base = RouteRequest("", candidates=1)
    for mode, expected in ((RouteMode.SPEED, PRESET), (RouteMode.ACCURACY, "greedier")):
        st = adjust_board_settings(BoardRouterSettings(base_request=base, budget_s=60), base, mode)
        wb = WorkingBoard(load_board(path).board, load_project_rules(path))
        res = BoardRouter(wb, replace(st, policy=_AlwaysGreedier())).run()
        tried = [o for o in res.outcomes.values() if o.attempts]
        assert tried
        for o in tried:
            assert o.arms == [expected] * o.attempts, (mode, o.net)
            assert [t["arm"] for t in o.trace] == o.arms


def test_wilson_rule_ignores_small_leads_and_needs_min_trials() -> None:
    ctx = "c"
    small = ThompsonPolicy({ctx: {PRESET: ArmStats(5, 8), "no_coarse": ArmStats(8, 8)}})
    assert small.eligible(ctx) == {} and small.choose(ctx, "n", 0) == PRESET  # 8/8 vs 5/8
    strong = ThompsonPolicy({ctx: {PRESET: ArmStats(10, 50), "no_coarse": ArmStats(40, 50)}})
    assert strong.eligible(ctx) == {"no_coarse": "routes significantly more"}
    assert strong.choose(ctx, "n", 0) == "no_coarse"
    # a lead that would survive the intervals still needs min_trials attempts
    few = {ctx: {PRESET: ArmStats(0, 50), "no_coarse": ArmStats(7, 7)}}
    assert ThompsonPolicy(few).eligible(ctx) == {}
    assert ThompsonPolicy(few, min_trials=7).eligible(ctx) == {
        "no_coarse": "routes significantly more"
    }


def test_much_cheaper_at_no_loss_is_eligible() -> None:
    ctx = "c"

    def pol(arm: ArmStats) -> ThompsonPolicy:
        return ThompsonPolicy({ctx: {PRESET: ArmStats(20, 40, 1.0, 40), "no_coarse": arm}})

    cheap = pol(ArmStats(20, 40, 0.4, 40))
    assert cheap.eligible(ctx) == {"no_coarse": "much cheaper at no loss"}
    assert cheap.choose(ctx, "n", 0) == "no_coarse"
    assert pol(ArmStats(20, 40, 0.6, 40)).eligible(ctx) == {}  # not cheap enough
    assert pol(ArmStats(19, 40, 0.1, 40)).eligible(ctx) == {}  # cheaper but loses
    assert pol(ArmStats(20, 40)).eligible(ctx) == {}  # no cost known
    assert pol(ArmStats(4, 7, 0.1, 7)).eligible(ctx) == {}  # below min_trials


def test_value_prefers_the_cheaper_of_equally_good_arms() -> None:
    ctx = "c"

    def pol(no_coarse_s: float, finer_s: float) -> ThompsonPolicy:
        return ThompsonPolicy(
            {
                ctx: {
                    PRESET: ArmStats(10, 50, 1.0, 50),
                    "no_coarse": ArmStats(40, 50, no_coarse_s, 50),
                    "finer_grid": ArmStats(40, 50, finer_s, 50),
                }
            }
        )

    assert set(pol(2.0, 0.5).eligible(ctx)) == {"no_coarse", "finer_grid"}
    assert pol(2.0, 0.5).choose(ctx, "n", 0) == "finer_grid"
    assert pol(0.5, 2.0).choose(ctx, "n", 0) == "no_coarse"
    p = pol(2.0, 0.5)
    assert p.value(ctx, "finer_grid") > p.value(ctx, "no_coarse")
    # routing more at ten times the cost does not beat the preset
    costly = ThompsonPolicy(
        {ctx: {PRESET: ArmStats(10, 50, 1.0, 50), "no_coarse": ArmStats(40, 50, 10.0, 50)}}
    )
    assert "no_coarse" in costly.eligible(ctx) and costly.choose(ctx, "n", 0) == PRESET


def test_retry_kind_groups_failure_reasons() -> None:
    from pcbrouter.routing.result import FailureReason

    assert retry_kind(None) == "unknown"
    for r in ("TIMEOUT", "VIA_LIMIT", "LAYER_RESTRICTION", "NODE_LIMIT", "CANCELLED"):
        assert retry_kind(r) == "limit", r
    assert retry_kind("NO_PATH") == retry_kind("no_path") == "no_path"
    assert retry_kind(FailureReason.TIMEOUT) == "limit"  # enums by value
    assert retry_kind(FailureReason.NO_PATH) == "no_path"
    assert retry_kind("VALIDATION") == retry_kind("CONGESTION") == "other"
    assert attempt_context("b", 0, "NO_PATH") == "b|first"  # first attempts ignore it
    assert attempt_context("b", 1, "NO_PATH") == "b|retry:no_path"
    assert attempt_context("b", 2) == "b|retry:unknown"


def test_attempts_read_retry_reasons_and_costs_from_the_trace() -> None:
    base = bucket("signal", 2, 8, 2)
    traced = _rec(["no_coarse", "finer_grid", PRESET], "SUCCESS")
    traced["outcome"]["route_s"] = 9.0  # the net's total: never an attempt's cost
    traced["outcome"]["trace"] = [
        {"reason": "NO_PATH", "route_s": 0.2},
        {"reason": "TIMEOUT", "route_s": 0.3},
        {"reason": None, "route_s": 0.1},
    ]
    assert list(attempts([traced])) == [
        Attempt(f"{base}|first", "no_coarse", 0.0, 0.2),
        Attempt(f"{base}|retry:no_path", "finer_grid", 0.0, 0.3),
        Attempt(f"{base}|retry:limit", PRESET, 1.0, 0.1),
    ]
    single = _rec(["greedier"], "SUCCESS")
    single["outcome"]["route_s"] = 0.7  # one attempt: the net's time is its cost
    assert list(attempts([single])) == [Attempt(f"{base}|first", "greedier", 1.0, 0.7)]
    untraced = _rec([PRESET, "greedier"], "NO_ROUTE")
    untraced["outcome"]["route_s"] = 0.7  # two attempts, no trace: not attributable
    assert list(attempts([untraced])) == [
        Attempt(f"{base}|first", PRESET, 0.0, None),
        Attempt(f"{base}|retry:unknown", "greedier", 0.0, None),
    ]


def test_bucket_has_a_routing_demand_band() -> None:
    assert DEMAND_EDGES == (0.03, 0.13)
    assert bucket("signal", 2, 8, 2) == "signal|p0|e2|l0|d?"  # unknown (old records)
    assert bucket("signal", 2, 8, 2, 0.01).endswith("|d0")
    assert bucket("signal", 2, 8, 2, 0.03).endswith("|d0")  # edges are inclusive below
    assert bucket("signal", 2, 8, 2, 0.05).endswith("|d1")
    assert bucket("signal", 2, 8, 2, 0.13).endswith("|d1")
    assert bucket("signal", 2, 8, 2, 0.5).endswith("|d2")
    # records carry the demand in the board profile
    rec = {**_rec(["greedier"], "SUCCESS"), "board_p": {"signal_layers": 2, "demand": 0.2}}
    assert next(attempts([rec])).ctx == "signal|p0|e2|l0|d2|first"
