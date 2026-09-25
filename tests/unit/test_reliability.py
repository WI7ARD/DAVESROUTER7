"""Stage 9 reliability: seeded fuzzing, properties, golden corpus, stress, security.

Every random test uses a fixed seed (reproducible) and a bounded number of cases.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
from pathlib import Path

import pytest

from pcbrouter.ai.command_parser import (
    CommandValidationError,
    parse_command_payload,
    parse_planner_response,
)
from pcbrouter.ai.exceptions import AIProviderError
from pcbrouter.domain.geometry import Point
from pcbrouter.kicad.errors import KiCadLoadError
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.kicad.writer import ExportError, compose
from pcbrouter.routing.connectivity import NetStatus
from pcbrouter.routing.request import RouteRequest
from pcbrouter.routing.router import Router
from pcbrouter.routing.working_board import CommitError, Provenance, WorkingBoard

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "tests" / "fixtures" / "boards"
GOLDEN = ROOT / "tests" / "fixtures" / "golden" / "corpus.json"
_CORPUS_PREFIXES = ("router_", "stage3_", "can_", "vias", "traces", "two_", "four_", "empty")
CORPUS = sorted(p.name for p in BOARDS.glob("*.kicad_pcb") if p.name.startswith(_CORPUS_PREFIXES))


def working(name: str) -> WorkingBoard:
    path = BOARDS / name
    return WorkingBoard(load_board(path).board, load_project_rules(path))


# ------------------------------------------------------------------ fuzzing
def _mutate(text: str, rng: random.Random) -> str:
    op = rng.randrange(5)
    i = rng.randrange(len(text))
    if op == 0:
        return text[:i]  # truncated file
    if op == 1:
        return text[:i] + text[i + rng.randrange(1, 200) :]  # deleted span
    if op == 2:
        return text[:i] + rng.choice(["(", ")", '"', "\x00", "(net", " 1e999 ", "nan"]) + text[i:]
    if op == 3:
        return re.sub(
            r"-?\d+\.\d+", lambda m: rng.choice([m.group(0), "-0", "1e30", "x"]), text, count=50
        )
    return text.replace("(", "", rng.randrange(1, 5))


def test_fuzz_malformed_kicad_never_crashes(tmp_path: Path) -> None:
    rng = random.Random(9001)
    base = (BOARDS / "router_basic.kicad_pcb").read_text(encoding="utf-8")
    loaded = 0
    for n in range(60):
        path = tmp_path / f"f{n}.kicad_pcb"
        path.write_text(_mutate(base, rng), encoding="utf-8", errors="surrogateescape")
        try:
            res = load_board(path)
        except KiCadLoadError:
            continue  # documented, user-facing error
        loaded += 1
        assert res.board is not None
        # the writer must refuse or produce text, never crash differently
        with contextlib.suppress(ExportError):
            compose(path.read_text(encoding="utf-8", errors="replace"), [], [], {})
    assert loaded < 60  # the corpus did exercise the error paths


def test_fuzz_ai_payloads_are_rejected_not_crashing() -> None:
    rng = random.Random(4242)
    pieces = [
        '{"',
        '"}',
        "[",
        "]",
        ",",
        ":",
        "null",
        "1e999",
        '"operation"',
        '"route_net"',
        '"net"',
        '"; rm -rf /"',
        '"__import__(\\"os\\")"',
        "{}",
        "true",
        "\ud800",
    ]
    for _ in range(300):
        text = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 25)))
        for fn in (parse_planner_response, parse_command_payload):
            with contextlib.suppress(AIProviderError, CommandValidationError, ValueError):
                fn(text)


def test_fuzz_validator_random_geometry_never_raises_and_legal_commits_stay_clean() -> None:
    rng = random.Random(77)
    wb = working("router_basic.kicad_pcb")
    xs = [p.position.x for p in wb.board.pads]
    ys = [p.position.y for p in wb.board.pads]
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    nets = sorted({p.net_name for p in wb.board.pads if p.net_name})
    committed = 0
    for i in range(150):
        net = rng.choice(nets)
        a = Point(rng.randint(lo_x, hi_x), rng.randint(lo_y, hi_y))
        b = Point(rng.randint(lo_x, hi_x), rng.randint(lo_y, hi_y))
        width = rng.choice([50_000, 150_000, 250_000, 400_000])
        res = wb.engine.validator.validate_segment(net, "B.Cu", a, b, width)
        if res.legal and committed < 5:
            from pcbrouter.domain.track import Track

            try:
                wb.commit_objects(
                    [Track(f"fz{i}", a, b, width, "B.Cu", net)],
                    [],
                    (),
                    "fuzz",
                    Provenance.USER_ACCEPTED,
                )
            except CommitError:
                continue  # the post-commit check is stricter (e.g. via holes): fine
            committed += 1
            assert not wb.engine.run_drc().errors  # property: legal stays legal


# ------------------------------------------------------------------ properties
def test_properties_undo_restores_fingerprint_and_connectivity_is_monotone() -> None:
    wb = working("router_basic.kicad_pcb")
    fps = [wb.fingerprint]
    connected = [
        sum(
            1 for c in wb.engine.connectivity.nets.values() if c.status is NetStatus.FULLY_CONNECTED
        )
    ]
    for net in ("A", "B", "C", "D"):
        res = Router(wb.engine).route_net(RouteRequest(net, candidates=1))
        if res.best is None:
            continue
        prop = res.best.proposal
        assert wb.engine.validator.validate_route(prop).legal  # accepted routes are valid
        wb.commit_proposals([prop], f"Route {net}")
        fps.append(wb.fingerprint)
        connected.append(
            sum(
                1
                for c in wb.engine.connectivity.nets.values()
                if c.status is NetStatus.FULLY_CONNECTED
            )
        )
        assert not wb.engine.run_drc().errors
    assert connected == sorted(connected) and connected[-1] > connected[0]
    for fp in reversed(fps[:-1]):
        wb.undo()
        assert wb.fingerprint == fp
    assert wb.board is wb.source


def test_reproducibility_same_input_same_route() -> None:
    a = Router(working("router_dense.kicad_pcb").engine).route_net(
        RouteRequest("S3", request_id="r", candidates=2)
    )
    b = Router(working("router_dense.kicad_pcb").engine).route_net(
        RouteRequest("S3", request_id="r", candidates=2)
    )
    assert [c.proposal.segments for c in a.candidates] == [
        c.proposal.segments for c in b.candidates
    ]
    assert [c.proposal.vias for c in a.candidates] == [c.proposal.vias for c in b.candidates]


# ------------------------------------------------------------------ golden corpus
def corpus_summary() -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for name in CORPUS:
        wb = working(name)
        drc = wb.engine.run_drc()
        conn = wb.engine.connectivity.nets
        out[name] = {
            "fingerprint": wb.board.fingerprint,
            "components": len(wb.board.components),
            "pads": len(wb.board.pads),
            "tracks": len(wb.board.tracks),
            "vias": len(wb.board.vias),
            "drc_errors": len(drc.errors),
            "drc_warnings": len(drc.warnings),
            "nets_connected": sum(
                1 for c in conn.values() if c.status is NetStatus.FULLY_CONNECTED
            ),
        }
    return out


def test_golden_corpus_is_stable() -> None:
    """Board facts and internal DRC counts for every fixture. Regenerate after an
    intended change with ``PCBROUTER_UPDATE_GOLDEN=1``."""
    import os

    current = corpus_summary()
    if os.environ.get("PCBROUTER_UPDATE_GOLDEN") == "1" or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("golden file written")
    assert current == json.loads(GOLDEN.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ stress
def test_stress_many_commits_and_undos_are_bounded_and_consistent() -> None:
    rng = random.Random(5)
    wb = working("router_basic.kicad_pcb")
    routes = {}
    for net in ("A", "B", "C"):
        res = Router(wb.engine).route_net(RouteRequest(net, candidates=1))
        assert res.best is not None
        routes[net] = res.best.proposal
    depth = 0
    for _ in range(120):
        if depth and rng.random() < 0.5:
            wb.undo()
            depth -= 1
        else:
            free = [n for n in routes if n not in {c.nets[0] for c in wb.commits if c.nets}]
            if not free:
                continue
            wb.commit_proposals([routes[rng.choice(free)]], "stress")
            depth += 1
        assert len(wb.commits) == depth
    while wb.commits:
        wb.undo()
    assert wb.board is wb.source


# ------------------------------------------------------------------ security audit
FORBIDDEN = [
    (re.compile(r"(?<![\w.])eval\("), "eval("),
    (re.compile(r"(?<![\w.])exec\("), "exec("),
    (re.compile(r"shell\s*=\s*True"), "shell=True"),
    (re.compile(r"os\.system\("), "os.system("),
    (re.compile(r"pickle\.loads?\("), "pickle"),
    (re.compile(r"\byaml\.load\("), "yaml.load("),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "hard-coded key"),
]


def test_security_audit_source_tree() -> None:
    hits = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern, label in FORBIDDEN:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line}: {label}")
    assert not hits, hits


def test_subprocess_calls_are_argument_lists_with_timeouts() -> None:
    for path in sorted((ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"subprocess\.(run|Popen|call|check_output)\(", text):
            call = text[m.start() : m.start() + 400]
            assert "timeout" in call or "Popen" in call, f"{path}: subprocess without timeout"
            assert not re.match(r"subprocess\.\w+\(\s*f?[\"']", call), f"{path}: string command"


def test_ai_modules_never_write_files_or_spawn_processes() -> None:
    """The AI layer produces validated commands only; file writes happen in export
    and settings code, never in response to raw model text."""
    for path in sorted((ROOT / "src" / "pcbrouter" / "ai").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "subprocess" not in text, path
        assert ".write_text(" not in text and ".write_bytes(" not in text, path
        assert "kicad.writer" not in text and "export_board" not in text, path
