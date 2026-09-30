"""Learning level 2: search-variant policies (no router needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.learning.policy import (
    ARMS,
    PRESET,
    ArmStats,
    RandomPolicy,
    ThompsonPolicy,
    attempt_context,
    bucket,
    learn,
    policy_from_spec,
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
    assert close.choose(ctx, "n", 0) == PRESET  # within the margin
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
