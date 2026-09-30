"""Experience-log schema freeze: golden records from real routing runs.

``tests/fixtures/experience/v1_sample.jsonl`` holds schema-1 records (no board
profile, no attempt trace) and ``v2_sample.jsonl`` schema-2 records, including a
net that needed two attempts. Readers must keep accepting both, and the learner
derives its contexts from the raw features, so old logs stay usable.

v2_sample.jsonl was produced by routing ``tests/fixtures/boards/router_dense``
in Accuracy mode with ``RandomPolicy(1)`` and a 10 s budget through
``BoardRouter`` and ``records_from_result(res, settings, salt="fixture",
source="fixture")``, keeping the multi-attempt net and two single-attempt nets.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from pcbrouter.learning.experience import ExperienceLog
from pcbrouter.learning.policy import Attempt, attempts

FIXTURES = Path(__file__).parent.parent / "fixtures" / "experience"

# Pinned learner view of the golden records. If the context definition changes
# deliberately (bucket bands, retry kinds, cost attribution), update these values
# AND docs/EXPERIENCE_SCHEMA.md in the same change.
V1_ATTEMPTS = [
    Attempt("signal|p0|e2|l0|d?|first", "finer_grid", 1.0, 0.072),
    Attempt("signal|p3|e0|l1|d?|first", "greedier", 0.0, None),
    Attempt("signal|p3|e0|l1|d?|retry:unknown", "no_coarse", 0.0, None),
    Attempt("diff_pair|p0|e2|l0|d?|first", "fewer_vias", 1.0, 0.044),
]
V2_ATTEMPTS = [
    Attempt("power|p0|e1|l0|d2|first", "greedier", 1.0, 0.0651),
    Attempt("signal|p0|e2|l0|d2|first", "preset", 1.0, 0.3882),
    Attempt("signal|p0|e2|l0|d2|first", "finer_grid", 0.0, 2.9622),
    Attempt("signal|p0|e2|l0|d2|retry:limit", "finer_grid", 1.0, 1.7554),
]


def _read(name: str, tmp_path: Path) -> list[dict[str, Any]]:
    """The fixture as the app reads it: copied into a log folder, via ExperienceLog."""
    folder = tmp_path / name
    folder.mkdir()
    shutil.copy(FIXTURES / f"{name}_sample.jsonl", folder / "experience.jsonl")
    return list(ExperienceLog(folder).read())


@pytest.mark.parametrize("name, schema", [("v1", 1), ("v2", 2)])
def test_golden_fixtures_load_through_the_log(name: str, schema: int, tmp_path: Path) -> None:
    recs = _read(name, tmp_path)
    assert len(recs) >= 2
    assert {r["schema"] for r in recs} == {f"pcbrouter-experience/{schema}"}


def test_schema_1_records_still_yield_attempts(tmp_path: Path) -> None:
    got = list(attempts(_read("v1", tmp_path)))
    assert got == V1_ATTEMPTS
    # no board profile: unknown demand band; no trace: unknown retry reason
    assert all("|d?|" in a.ctx for a in got)
    assert any(a.ctx.endswith("|retry:unknown") for a in got)


def test_schema_2_records_yield_attempts_with_costs_and_retry_reasons(tmp_path: Path) -> None:
    recs = _read("v2", tmp_path)
    assert any(len(r["outcome"]["arms"]) >= 2 for r in recs), "needs a multi-attempt net"
    got = list(attempts(recs))
    assert got == V2_ATTEMPTS
    assert all(a.cost_s is not None for a in got)  # every attempt's time is traced
    assert all("|d?|" not in a.ctx for a in got)  # the profile gives the demand band
    retries = [a for a in got if "|retry:" in a.ctx]
    assert retries and all(not a.ctx.endswith(":unknown") for a in retries)


def test_schema_2_required_fields(tmp_path: Path) -> None:
    for r in _read("v2", tmp_path):
        for key in ("schema", "app", "router", "board", "board_f", "board_p", "job", "net_f"):
            assert key in r, key
        assert {"settings", "outcome"} <= set(r)
        assert {"app", "geometry", "rules"} <= set(r["router"])
        assert {"signal_layers", "demand"} <= set(r["board_p"])
        assert {"kind", "pads", "escape_options", "bucket"} <= set(r["net_f"])
        assert {"mode", "grid_mm", "heuristic_weight", "coarse_factor"} <= set(r["settings"])
        out = r["outcome"]
        assert {"status", "arms", "policy", "route_s", "trace"} <= set(out)
        assert len(out["trace"]) == len(out["arms"]) == out["attempts"]
        for t in out["trace"]:
            assert {"pass", "grid_mm", "heuristic_weight", "coarse_factor", "status"} <= set(t)
            assert {"reason", "route_s", "arm"} <= set(t)
