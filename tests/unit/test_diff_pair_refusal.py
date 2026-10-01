"""Explicit diff-pair rules KiCad enforces (gap max, uncoupled max, gap min above
clearance) are refused precisely for pair nets: the router does not route
coupled pairs, so it must not silently produce copper KiCad rejects.
(KiCad: drc_test_provider_diff_pair_coupling, verified with KiCad 8.0.8.)"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbrouter.kicad import oracle
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.result import FailureReason
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import WorkingBoard
from tests.support import kicadgen as gen


def _rule(constraint: str, condition: str = "A.inDiffPair('*')", severity: str = "") -> str:
    sev = f" (severity {severity})" if severity else ""
    return f'(version 1)\n(rule dp{sev} (constraint {constraint}) (condition "{condition}"))\n'


def _engine(tmp_path: Path, dru: str, **kw: object) -> WorkingBoard:
    opts = {"net_a": "USBP", "net_b": "USBN", "gap_mm": 0.5, **kw}
    path = gen.board(tmp_path, dru=dru, **opts)  # type: ignore[arg-type]
    return WorkingBoard(load_board(path).board, load_project_rules(path))


@pytest.mark.parametrize(
    ("dru", "refused"),
    [
        (_rule("diff_pair_gap (min 0.1mm) (max 0.2mm)"), "at most 0.2 mm"),
        (_rule("diff_pair_uncoupled (max 1mm)"), "uncoupled length to 1 mm"),
        (_rule("diff_pair_gap (min 0.5mm)"), "at least 0.5 mm"),  # above 0.2 mm clearance
        (_rule("diff_pair_gap (min 0.1mm)"), None),  # ordinary clearance already meets it
        (_rule("diff_pair_gap (max 0.2mm)", severity="warning"), None),  # not a KiCad error
        (_rule("diff_pair_gap (max 0.2mm)", "A.inDiffPair('CLK')"), None),  # other pairs
    ],
)
def test_refusal_names_the_rule_only_when_kicad_would_flag(
    tmp_path: Path, dru: str, refused: str | None
) -> None:
    wb = _engine(tmp_path, dru)
    reason = wb.engine.resolver.diff_pair_refusal("USBP")
    if refused is None:
        assert reason is None
    else:
        assert reason is not None and refused in reason and "rule 'dp'" in reason
        assert "USBP/USBN" in reason


def test_nets_outside_a_pair_are_never_refused(tmp_path: Path) -> None:
    wb = _engine(tmp_path, _rule("diff_pair_gap (max 0.2mm)", "A.NetName == '*'"), net_b="GND")
    assert wb.engine.resolver.diff_pair_refusal("USBP") is None  # partner USBN absent
    assert wb.engine.resolver.diff_pair_refusal("GND") is None


def test_router_reports_unsupported_rule_instead_of_routing(tmp_path: Path) -> None:
    wb = _engine(tmp_path, _rule("diff_pair_gap (max 0.2mm)"))
    result = Router(wb.engine).route_net(RouteRequest("USBP", candidates=1))
    assert result.reason is FailureReason.UNSUPPORTED_RULE
    assert "coupled diff-pair routing is not supported" in (result.message or "")


_TOOL = oracle.find_oracle()


@pytest.mark.skipif(_TOOL is None, reason="KiCad 8+ kicad-cli not installed")
def test_kicad_flags_independent_parallel_pair_tracks(tmp_path: Path) -> None:
    assert _TOOL is not None
    path = gen.board(
        tmp_path, net_a="USBP", net_b="USBN", gap_mm=0.5,
        dru=_rule("diff_pair_gap (min 0.1mm) (max 0.2mm)"),
    )  # fmt: skip
    run = oracle.run_drc(_TOOL, path, "as_exported")
    assert ("diff_pair_gap_out_of_range", "error") in {(v.type, v.severity) for v in run.violations}
