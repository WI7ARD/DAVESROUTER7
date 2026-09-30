"""Learning level 2 trainer: data selection, grouped board split, replay evaluation,
support/evidence, policy sets, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbrouter.learning.experience import ExperienceLog
from pcbrouter.learning.policy import (
    PRESET,
    ArmStats,
    FixedPolicy,
    PolicySet,
    ThompsonPolicy,
    attempt_context,
    bucket,
    learn,
    policy_from_spec,
)
from pcbrouter.learning.selector import SelectivePolicy
from pcbrouter.learning.trainer import (
    Estimate,
    TrainConfig,
    attach_evidence,
    board_groups,
    compare,
    dataset_id,
    is_held_out,
    main,
    replay,
    select,
    split_by_board,
    train,
)

#: the context every synthetic net below lands in (2-pad signal, 2 routable layers)
CTX = attempt_context(bucket("signal", 2, 8, 2), 0)


def _profile(b: int, twin_of: int | None = None) -> dict[str, float]:
    """A distinct board profile per index (a twin copies another board's)."""
    k = twin_of if twin_of is not None else b
    return {
        "copper_layers": 2.0,
        "signal_layers": 2.0,
        "area_cm2": 10.0 + 7.0 * k,
        "nets_to_route": 5.0 + 3.0 * k,
        "pads_per_net": 2.0 + 0.1 * (k % 7),
        "pad_density_cm2": 1.0 + 0.2 * (k % 5),
        "pitch_p10_mm": 0.5 + 0.1 * (k % 3),
        "airwire_mean_mm": 10.0 + k,
        "long_net_frac": (k % 10) / 10,
        "demand": 0.05 + 0.01 * k,
        "low_escape_frac": (k % 4) / 4,
        "congestion_mean": 0.6,
        "multi_pad_frac": 0.1,
    }


def _rec(
    board: str,
    arms: list[str],
    ok: bool,
    *,
    policy: str = "random",
    mode: str = "speed",
    kind: str = "signal",
    profile: dict[str, float] | None = None,
) -> dict:
    return {
        "board": board,
        "board_p": profile if profile is not None else _profile(int(board.lstrip("B") or 0)),
        "net_f": {"kind": kind, "pads": 2, "escape_options": 8},
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


def _world(boards: int = 20, mode: str = "speed") -> list[dict]:
    """ "finer_grid" routes every first attempt, the preset half of them."""
    recs = []
    for b in range(boards):
        for i in range(10):
            recs.append(_rec(f"B{b}", ["finer_grid"], True, mode=mode))
            recs.append(_rec(f"B{b}", [PRESET], i % 2 == 0, mode=mode))
            recs.append(_rec(f"B{b}", ["greedier"], i % 4 == 0, mode=mode))
    return recs


def test_only_exploration_records_with_arms_are_used() -> None:
    recs = [
        _rec("B0", ["greedier"], True),
        _rec("B0", ["greedier"], True, policy="thompson"),  # biased logging
        _rec("B0", [], True, policy="fixed"),  # no arms: teaches nothing
        _rec("B0", ["greedier"], True, mode="accuracy"),
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


def test_near_duplicate_boards_are_grouped_and_never_split() -> None:
    # B99 is a variant of B3 (same profile): it must land on B3's side, always
    recs = [*_world(30), _rec("B99", ["finer_grid"], True, profile=_profile(99, twin_of=3))]
    groups = board_groups(recs)
    assert groups["B99"] == groups["B3"] and len(set(groups.values())) == 30
    for seed in range(20):
        train_recs, held = split_by_board(recs, 0.5, seed)
        sides = {r["board"]: r in held for r in train_recs + held if r["board"] in ("B3", "B99")}
        assert sides["B3"] == sides["B99"]


def test_contexts_use_routable_layers_not_copper_layers() -> None:
    # a 4-copper board with two planes routes like a 2-layer board...
    planes = _rec("B1", ["greedier"], True, profile={**_profile(1), "signal_layers": 2.0})
    planes["board_f"] = {"copper_layers": 4}
    # ...a 4-copper board with four free layers does not
    free = _rec("B2", ["greedier"], True, profile={**_profile(2), "signal_layers": 4.0})
    free["board_f"] = {"copper_layers": 4}
    stats = learn([planes, free]).stats
    assert set(stats) == {CTX, attempt_context(bucket("signal", 2, 8, 4), 0)}


def test_replay_scores_only_attempts_the_policy_would_have_made() -> None:
    recs = _world(4)
    fine = ThompsonPolicy({CTX: {"finer_grid": ArmStats(1, 1)}}, min_trials=0, margin=0)
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
    assert res.policy.choose(CTX, "", 0) == "finer_grid"
    assert res.report["changed_contexts"] == {CTX: "finer_grid"}
    h = res.report["held_out"]
    assert h["policy"]["rate"] == 1.0 and h["preset"]["rate"] == 0.5
    assert h["policy_minus_preset"]["verdict"] == "better"
    assert res.report["arm_costs"]["finer_grid"]["median_s"] == 0.5
    # the trainer's counts agree with policy.learn on the same training data
    train_recs, _ = split_by_board(_world(30), 0.3, 2)
    assert res.policy.to_json()["stats"] == learn(train_recs).to_json()["stats"]
    # identity: policy content hash and training-data hash are recorded
    assert res.report["policy_id"] == res.policy.policy_id()
    assert res.report["dataset_id"] == dataset_id(train_recs)
    assert res.support is not None and len(res.support.boards) == res.report["boards"]["train"]


def test_dataset_id_ignores_order_and_changes_with_content() -> None:
    recs = _world(3)
    assert dataset_id(recs) == dataset_id(list(reversed(recs)))
    assert dataset_id(recs) != dataset_id(recs[:-1])


def test_evidence_attaches_only_to_training_boards() -> None:
    res = train(_world(30), TrainConfig(holdout=0.3, seed=2))
    held = set(res.report["held_out_boards"])
    trained = sorted(set(res.report["train_boards"]))
    ev = [
        {"profile": _profile(int(trained[0].lstrip("B"))), "mode": "speed", "delta_mean": 3.0},
        # measured on a held-out board: must be ignored (evaluation data)
        {"profile": _profile(int(sorted(held)[0].lstrip("B"))), "mode": "speed", "delta_mean": 9},
    ]
    assert attach_evidence(res.support, ev) == 1
    assert sum(1 for b in res.support.boards if b.evidence) == 1


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
    assert pol.choose(CTX, "", 0) == "finer_grid"
    saved = json.loads(out.read_text())
    assert saved["trained"]["records"]["usable"] == 900
    assert saved["trained"]["policy_id"] == pol.policy_id() and saved["support"]["boards"]
    assert "B1" not in out.read_text()  # counts and profiles only, no board ids
    assert isinstance(policy_from_spec(str(out)), SelectivePolicy)
    assert json.loads(rep.read_text())["held_out"]["policy_minus_preset"]["verdict"] == "better"
    assert "held-out replay" in capsys.readouterr().out


def test_cli_per_mode_writes_one_policy_set(tmp_path: Path) -> None:
    ExperienceLog(tmp_path / "exp").append(_world(20) + _world(20, mode="accuracy"))
    out = tmp_path / "set.json"
    assert main(["--log", str(tmp_path / "exp"), "--out", str(out), "--per-mode"]) == 0
    pol = policy_from_spec(str(out))
    assert isinstance(pol, PolicySet)
    assert pol.for_mode("speed") is not None and pol.for_mode("accuracy") is not None
    assert pol.for_mode("other") is None and pol.policy_id().startswith("set-")


def test_cli_refuses_without_exploration_data(tmp_path: Path) -> None:
    ExperienceLog(tmp_path / "exp").append([_rec("B0", [PRESET], True, policy="fixed")])
    assert main(["--log", str(tmp_path / "exp"), "--out", str(tmp_path / "p.json")]) == 2
    assert not (tmp_path / "p.json").exists()
    assert main(["--log", str(tmp_path / "exp"), "--holdout", "1.5"]) == 2
