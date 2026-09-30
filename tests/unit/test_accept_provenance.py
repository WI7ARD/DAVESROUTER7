"""Copper the user accepted is user-approved, not speculative (1.1.1, P1-1).

Accepting a board-routing batch (all or some nets) records USER_ACCEPTED
provenance. A later job may only rip that copper up when the settings say
``ripup_user_accepted``; by default it stays exactly where the user accepted it.
"""

from __future__ import annotations

from pathlib import Path

from pcbrouter.routing.working_board import Provenance
from tests.unit.test_partial_acceptance import Session, x_then_y


def _provenance(s: Session, net: str) -> set[Provenance]:
    ids = [t.id for t in s.working.board.tracks if t.net_name == net]
    ids += [v.id for v in s.working.board.vias if v.net_name == net]
    assert ids, f"{net} has no copper"
    return {s.working.provenance_of(i) for i in ids}


def test_accepting_a_whole_batch_records_user_accepted(tmp_path: Path) -> None:
    s = Session(tmp_path)
    res = s.route(["X"], allow_ripup=False)
    assert s.accept(res, None).success  # type: ignore[attr-defined]
    assert _provenance(s, "X") == {Provenance.USER_ACCEPTED}


def test_partial_acceptance_marks_pulled_in_nets_user_accepted(tmp_path: Path) -> None:
    s, res = x_then_y(tmp_path)
    assert s.accept(res, {"Y"}).success  # type: ignore[attr-defined]
    assert _provenance(s, "Y") == {Provenance.USER_ACCEPTED}
    assert _provenance(s, "X") == {Provenance.USER_ACCEPTED}  # moved for Y, accepted with it


def test_accepted_copper_is_protected_from_ripup_by_default(tmp_path: Path) -> None:
    s = Session(tmp_path)
    first = s.route(["X"], allow_ripup=False)
    assert s.accept(first, None).success  # type: ignore[attr-defined]
    x_ids = {t.id for t in s.working.board.tracks}
    res = s.route(["Y"], allow_ripup=True)  # ripup_user_accepted defaults to False
    assert not res.removed_ids and res.metrics.ripups == 0
    assert x_ids <= {t.id for t in res.final_board.tracks}
    assert res.outcomes["Y"].status.value != "SUCCESS"


def test_ripup_of_accepted_copper_needs_the_explicit_setting(tmp_path: Path) -> None:
    _s, res = x_then_y(tmp_path)  # ripup_user_accepted=True
    assert res.removed_ids and set(res.removed_nets.values()) == {"X"}
