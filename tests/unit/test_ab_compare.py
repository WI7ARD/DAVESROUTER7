"""Paired A/B statistics and the policy acceptance gate (pcbrouter.benchmark.ab)."""

from __future__ import annotations

from pcbrouter.benchmark import ab


def _row(done: int, *, status: str = "ok", vias: int = 10, drc: int = 0) -> dict:
    return {
        "status": status,
        "metrics": {"nets_completed": done, "nets_attempted": 50, "new_vias": vias},
        "wall_s": 10.0,
        "new_copper_drc_errors": drc,
        "policy_decision": {"decision": "LEARNED"},
    }


def _runs(table: dict[str, list[int]]) -> list[dict]:
    """{board: [nets per repeat]} -> one Real100-like run dict per repeat."""
    n = len(next(iter(table.values())))
    return [{(b, "speed"): _row(v[i]) for b, v in table.items()} for i in range(n)]


def test_pairs_classify_consistent_noisy_and_unchanged_boards() -> None:
    base = _runs({"A": [10, 10, 10], "B": [20, 20, 20], "C": [30, 30, 30], "D": [5, 5, 5]})
    cand = _runs({"A": [12, 13, 12], "B": [18, 19, 19], "C": [33, 27, 31], "D": [5, 5, 5]})
    got = {p.key[0]: p.verdict for p in ab.pair_runs(base, cand)}
    assert got == {"A": "improved", "B": "regressed", "C": "noisy", "D": "unchanged"}


def test_sign_test_is_exact() -> None:
    base = _runs({f"B{i}": [0] for i in range(6)})
    cand = _runs({f"B{i}": [1] for i in range(6)})
    assert ab.sign_test(ab.pair_runs(base, cand)) == 2 / 64  # 6 of 6 positive
    assert ab.sign_test([]) == 1.0


def test_gate_rejects_any_regression_even_with_a_big_aggregate_gain() -> None:
    base = _runs({"A": [10, 10], "B": [50, 50]})
    cand = _runs({"A": [60, 61], "B": [48, 49]})
    g = ab.gate(ab.pair_runs(base, cand), repeats=2)
    assert g["aggregate"]["net_delta"] > 0 and g["status"] == "REJECTED"
    assert not g["checks"]["2_no_regressed_board"]


def test_gate_rejects_guard_regressions_validity_loss_and_new_failures() -> None:
    base, cand = _runs({"A": [10, 10]}), _runs({"A": [20, 20]})
    good = ab.pair_runs(base, cand)
    guard = ab.pair_runs(_runs({"g": [44, 44]}), _runs({"g": [39, 38]}))
    assert ab.gate(good, guard, 2)["status"] == "REJECTED"
    bad_drc = [{k: {**v, "new_copper_drc_errors": 1} for k, v in r.items()} for r in cand]
    assert ab.gate(ab.pair_runs(base, bad_drc), repeats=2)["status"] == "REJECTED"
    crashed = [{k: {**v, "status": "timeout"} for k, v in r.items()} for r in cand]
    assert ab.gate(ab.pair_runs(base, crashed), repeats=2)["status"] == "REJECTED"


def test_gate_accepts_only_reproduced_gains_otherwise_experimental() -> None:
    base, cand = _runs({"A": [10, 10], "B": [10, 10]}), _runs({"A": [15, 14], "B": [10, 10]})
    assert ab.gate(ab.pair_runs(base, cand), repeats=2)["status"] == "ACCEPTED"
    one = ab.gate(ab.pair_runs(base[:1], cand[:1]), repeats=1)
    assert one["status"] == "EXPERIMENTAL" and not one["checks"]["5_reproducible"]
    flat = ab.gate(ab.pair_runs(base, base), repeats=2)
    assert flat["status"] == "EXPERIMENTAL"  # no harm, no benefit


def test_guard_rows_parse_run_benchmarks_results() -> None:
    rows = ab.guard_rows([
        {"board": "medium_4layer", "mode": "speed", "nets": "40/44", "vias": 118,
         "exit_code": 0, "wall_s": 111.0, "verified": True},
        {"board": "dense_2layer", "mode": "speed", "nets": "?/?", "exit_code": -9},
    ])  # fmt: skip
    m = rows[("medium_4layer", "speed")]
    assert m["status"] == "ok" and m["metrics"]["nets_completed"] == 40
    assert rows[("dense_2layer", "speed")]["status"] != "ok"
