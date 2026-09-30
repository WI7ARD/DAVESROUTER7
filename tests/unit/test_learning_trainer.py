"""Learning level 2 trainer: data selection, board split, replay evaluation, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbrouter.learning.experience import ExperienceLog
from pcbrouter.learning.policy import PRESET, ArmStats, FixedPolicy, ThompsonPolicy, learn
from pcbrouter.learning.trainer import (
    Estimate,
    TrainConfig,
    compare,
    is_held_out,
    main,
    replay,
    select,
    split_by_board,
    train,
)


def _rec(
    board: str,
    arms: list[str],
    ok: bool,
    *,
    policy: str = "random",
    mode: str = "speed",
    kind: str = "signal",
) -> dict:
    return {
        "board": board,
        "net_f": {"kind": kind, "pads": 2, "escape_options": 8, "bucket": f"{kind}|b"},
        "board_f": {"copper_layers": 2},
        "settings": {"mode": mode},
        "outcome": {
            "arms": arms,
            "policy": policy,
            "status": "SUCCESS" if ok else "NO_ROUTE",
            "route_s": 0.5 if arms == ["finer_grid"] else 0.1,
            "expanded_nodes": 100,
        },
    }


def _world(boards: int = 20) -> list[dict]:
    """ "finer_grid" routes every first attempt, the preset half of them."""
    recs = []
    for b in range(boards):
        for i in range(10):
            recs.append(_rec(f"B{b}", ["finer_grid"], True))
            recs.append(_rec(f"B{b}", [PRESET], i % 2 == 0))
            recs.append(_rec(f"B{b}", ["greedier"], i % 4 == 0))
    return recs


def test_only_exploration_records_with_arms_are_used() -> None:
    recs = [
        _rec("a", ["greedier"], True),
        _rec("a", ["greedier"], True, policy="thompson"),  # biased logging
        _rec("a", [], True, policy="fixed"),  # no arms: teaches nothing
        _rec("a", ["greedier"], True, mode="accuracy"),
    ]
    assert len(select(recs, TrainConfig())) == 2
    assert len(select(recs, TrainConfig(modes=("speed",)))) == 1
    assert len(select(recs, TrainConfig(policies=("random", "thompson")))) == 3


def test_split_is_by_board_and_deterministic() -> None:
    recs = _world(40)
    train_recs, held = split_by_board(recs, 0.3, seed=1)
    tb, hb = {r["board"] for r in train_recs}, {r["board"] for r in held}
    assert tb and hb and not tb & hb  # no board on both sides
    assert 0.1 < len(hb) / 40 < 0.5
    assert split_by_board(recs, 0.3, seed=1) == (train_recs, held)
    assert all(not is_held_out(f"B{b}", 0.0, 1) for b in range(40))


def test_replay_scores_only_attempts_the_policy_would_have_made() -> None:
    recs = _world(4)
    ctx = "signal|b|first"
    fine = ThompsonPolicy({ctx: {"finer_grid": ArmStats(1, 1)}}, min_trials=0, margin=0)
    est = replay(fine, recs)
    assert (est.n, est.rate) == (40, 1.0)
    base = replay(FixedPolicy(), recs)
    assert (base.n, base.rate) == (40, 0.5)


def test_intervals_and_comparison_are_honest() -> None:
    lo, hi = Estimate(5, 10).wilson95()
    assert 0.2 < lo < 0.5 < hi < 0.8
    assert Estimate().wilson95() == (0.0, 1.0)
    assert compare(Estimate(95, 100), Estimate(50, 100))["verdict"] == "better"
    assert compare(Estimate(6, 10), Estimate(5, 10))["verdict"] == "no clear difference"
    assert compare(Estimate(), Estimate(1, 2))["verdict"] == "no data"


def test_train_learns_the_better_arm_and_it_wins_on_held_out_boards() -> None:
    res = train(_world(30), TrainConfig(holdout=0.3, seed=2))
    assert res.policy.choose("signal|b|first", "", 0) == "finer_grid"
    assert res.report["changed_contexts"] == {"signal|b|first": "finer_grid"}
    h = res.report["held_out"]
    assert h["policy"]["rate"] == 1.0 and h["preset"]["rate"] == 0.5
    assert h["policy_minus_preset"]["verdict"] == "better"
    assert res.report["arm_costs"]["finer_grid"]["median_s"] == 0.5
    # the trainer's counts agree with policy.learn on the same training data
    train_recs, _ = split_by_board(_world(30), 0.3, 2)
    assert res.policy.to_json()["stats"] == learn(train_recs).to_json()["stats"]


def test_cli_trains_from_logs_and_writes_a_loadable_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = ExperienceLog(tmp_path / "exp")
    log.append(_world(30))
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"torn": ')
    out, rep = tmp_path / "p.json", tmp_path / "r.json"
    code = main(["--log", str(tmp_path / "exp"), "--out", str(out), "--report", str(rep)])
    assert code == 0
    pol = ThompsonPolicy.load(out)
    assert pol.choose("signal|b|first", "", 0) == "finer_grid"
    saved = json.loads(out.read_text())
    assert saved["trained"]["records"]["usable"] == 900
    assert "B1" not in out.read_text()  # counts only, no board ids in the policy
    assert json.loads(rep.read_text())["held_out"]["policy_minus_preset"]["verdict"] == "better"
    assert "held-out replay" in capsys.readouterr().out


def test_cli_refuses_without_exploration_data(tmp_path: Path) -> None:
    ExperienceLog(tmp_path / "exp").append([_rec("a", [PRESET], True, policy="fixed")])
    assert main(["--log", str(tmp_path / "exp"), "--out", str(tmp_path / "p.json")]) == 2
    assert not (tmp_path / "p.json").exists()
    assert main(["--log", str(tmp_path / "exp"), "--holdout", "1.5"]) == 2
