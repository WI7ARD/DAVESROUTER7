from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.history import (
    HistoryManager,
    MetadataAction,
    MetadataSnapshotStore,
    ProposalStateError,
    ProposalStatus,
    RouteProposal,
)


def test_push_undo_redo_cycle() -> None:
    history = HistoryManager()
    a = MetadataAction("first", {"n": 1})
    b = MetadataAction("second")
    history.push(a)
    history.push(b)
    assert a.applied and b.applied
    assert history.undo_label == "second" and history.can_undo and not history.can_redo

    assert history.undo() is not None
    assert not b.applied and a.applied
    assert history.redo_label == "second"
    assert history.redo() is not None and b.applied

    history.undo()
    history.undo()
    assert not a.applied and history.undo() is None
    assert [e.label for e in history.entries()] == []


def test_push_clears_redo_and_records_metadata() -> None:
    history = HistoryManager()
    history.push(MetadataAction("a"))
    history.undo()
    entry = history.push(MetadataAction("b", {"net": "GND"}), already_applied=True)
    assert not history.can_redo
    assert entry.metadata == {"net": "GND"} and entry.kind == "MetadataAction"
    assert entry.sequence == 2


def test_max_depth_and_listeners() -> None:
    history = HistoryManager(max_depth=3)
    calls: list[int] = []
    history.subscribe(lambda: calls.append(1))
    for i in range(5):
        history.push(MetadataAction(f"a{i}"))
    assert [e.label for e in history.entries()] == ["a2", "a3", "a4"]
    history.clear()
    assert not history.can_undo
    assert len(calls) == 6
    with pytest.raises(ValueError):
        HistoryManager(max_depth=0)


def test_snapshot_store_is_metadata_only(tmp_path: Path) -> None:
    store = MetadataSnapshotStore()
    ref = store.create(tmp_path / "b.kicad_pcb", "ab" * 32, "before routing")
    assert ref.storage_path is None
    assert store.get(ref.id) == ref and store.list_snapshots() == [ref]
    assert store.get("missing") is None
    assert list(tmp_path.iterdir()) == []  # nothing written


def test_route_proposal_transitions() -> None:
    p = RouteProposal("p1", "route GND", "00" * 32, added_tracks=3)
    accepted = p.accept()
    assert accepted.status is ProposalStatus.ACCEPTED and p.status is ProposalStatus.PENDING
    with pytest.raises(ProposalStateError):
        accepted.reject()
    assert p.reject().status is ProposalStatus.REJECTED


class _Boom(MetadataAction):
    def __init__(self, label: str, *, fail_on: str) -> None:
        super().__init__(label)
        self._fail_on = fail_on

    def apply(self) -> None:
        if self._fail_on == "apply":
            raise RuntimeError("boom-apply")
        super().apply()

    def revert(self) -> None:
        if self._fail_on == "revert":
            raise RuntimeError("boom-revert")
        super().revert()


def test_failed_push_records_nothing_and_keeps_redo() -> None:
    history = HistoryManager()
    history.push(MetadataAction("good"))
    history.undo()
    assert history.can_redo
    with pytest.raises(RuntimeError, match="boom-apply"):
        history.push(_Boom("bad", fail_on="apply"))
    assert [e.label for e in history.entries()] == []
    assert history.can_redo and history.redo_label == "good"


def test_failed_undo_keeps_entry_on_undo_stack() -> None:
    history = HistoryManager()
    history.push(_Boom("fragile", fail_on="revert"))
    with pytest.raises(RuntimeError, match="boom-revert"):
        history.undo()
    assert [e.label for e in history.entries()] == ["fragile"]
    assert history.can_undo and not history.can_redo


def test_failed_redo_keeps_entry_on_redo_stack() -> None:
    history = HistoryManager()
    action = _Boom("fragile", fail_on="apply")
    history.push(action, already_applied=True)
    history.undo()  # revert succeeds (fail_on is apply)
    with pytest.raises(RuntimeError, match="boom-apply"):
        history.redo()
    assert history.can_redo and not history.can_undo
    assert history.redo_label == "fragile"


def test_throwing_listener_does_not_break_history() -> None:
    history = HistoryManager()
    calls: list[str] = []

    def bad() -> None:
        raise RuntimeError("listener boom")

    history.subscribe(bad)
    history.subscribe(lambda: calls.append("ok"))
    history.push(MetadataAction("a"))
    assert calls == ["ok"]
    assert history.undo() is not None and calls == ["ok", "ok"]
