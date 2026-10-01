"""OpenBoards: a second pinned benchmark corpus run by the Real100 harness.

No network: downloads and git calls are monkeypatched.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from pcbrouter.benchmark import real100
from pcbrouter.benchmark.real100 import (
    OPENBOARDS_SCHEMA,
    RAW_BASE,
    _raw_url,
    build_parser,
    fetch_board,
    git_blob_sha1,
    load_manifest,
    prepare_board,
    select_specs,
)

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "tests" / "fixtures" / "boards"
OPENBOARDS = ROOT / "benchmarks" / "openboards"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def curate() -> ModuleType:
    return _load_module("openboards_curate", OPENBOARDS / "curate.py")


@pytest.fixture(scope="module")
def compare() -> ModuleType:
    return _load_module("real100_compare", ROOT / "tools" / "real100_compare.py")


def _board(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"O{i:03d}",
        "name": f"b{i}.kicad_pcb",
        "family": "keyboard",
        "source_path": f"pcb/b{i}.kicad_pcb",
        "source_size_bytes": 10,
        "git_blob_sha1": "0" * 40,
        "difficulty": "small",
        **extra,
    }


def _manifest(tmp_path: Path, boards: list[dict[str, Any]], **top: Any) -> Path:
    data = {
        "schema": OPENBOARDS_SCHEMA,
        "suite_name": "OpenBoards test",
        "suite_version": "0",
        "boards": boards,
        **top,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ------------------------------------------------------------ manifest parsing
def test_openboards_manifest_per_board_repository_ref_and_sidecars(tmp_path: Path) -> None:
    side = [{"path": "pcb/b1.kicad_pro", "git_blob_sha1": "1" * 40}]
    path = _manifest(
        tmp_path,
        [
            _board(1, repository="acme/kbd", commit=COMMIT_A, sidecars=side, license="MIT",
                   reference_complete=True),
            _board(2),
        ],
        source_repository="https://github.com/other/repo",
        source_ref=COMMIT_B,
    )  # fmt: skip
    m = load_manifest(path)
    assert not m.is_real100 and m.slug == "openboards"
    b1, b2 = m.boards
    assert (b1.repository, b1.ref) == ("acme/kbd", COMMIT_A)
    assert b1.sidecars == (("pcb/b1.kicad_pro", "1" * 40),)
    assert b1.license == "MIT" and b1.reference_complete is True
    # per-board fields fall back to the manifest-level source
    assert (b2.repository, b2.ref) == ("https://github.com/other/repo", COMMIT_B)
    assert b2.sidecars == () and b2.reference_complete is None
    assert m.board_repository(b2) == "https://github.com/other/repo"


def test_board_without_any_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repository"):
        load_manifest(_manifest(tmp_path, [_board(1)]))


def test_hundred_board_rule_applies_only_to_real100(tmp_path: Path) -> None:
    boards = [_board(i, repository="acme/kbd", commit=COMMIT_A) for i in (1, 2)]
    assert len(load_manifest(_manifest(tmp_path, boards)).boards) == 2
    real = _manifest(tmp_path, boards, schema="davesrouter-real100/1.1", source_ref_date="x")
    with pytest.raises(ValueError, match="exactly 100"):
        load_manifest(real)


def test_openboards_ids_must_be_unique_and_non_empty(tmp_path: Path) -> None:
    dup = [_board(1, repository="a/b", commit=COMMIT_A)] * 2
    with pytest.raises(ValueError, match="unique"):
        load_manifest(_manifest(tmp_path, dup))
    with pytest.raises(ValueError, match="at least one"):
        load_manifest(_manifest(tmp_path, []))


def test_unknown_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="schema"):
        load_manifest(_manifest(tmp_path, [_board(1)], schema="nope/1"))


def test_committed_openboards_manifest_is_pinned() -> None:
    m = load_manifest(OPENBOARDS / "manifest.json")
    assert m.schema == OPENBOARDS_SCHEMA and 40 <= len(m.boards) <= 60
    assert [b.id for b in m.boards] == [f"O{i:03d}" for i in range(1, len(m.boards) + 1)]
    for b in m.boards:
        assert b.repository and b.repository.count("/") == 1
        assert b.ref and len(b.ref) == 40 and len(b.git_blob_sha1) == 40
        assert b.license and b.reference_complete is not None
        assert any(p.endswith(".kicad_pro") for p, _sha in b.sidecars)
        assert all(len(sha) == 40 for _p, sha in b.sidecars)


# ----------------------------------------------------------------- raw URLs
def test_raw_url_is_per_repository_and_real100_url_is_unchanged() -> None:
    m = load_manifest()  # Real100
    spec = m.boards[0]
    url = _raw_url(m.board_repository(spec), m.board_ref(spec), spec.source_path)
    assert url == f"{RAW_BASE}/{m.source_ref}/{spec.source_path}"
    assert url.startswith("https://raw.githubusercontent.com/KiCad/kicad-source-mirror/")
    assert (
        _raw_url("acme/kbd", COMMIT_A, "Sweep v2/a b.kicad_pcb")
        == f"https://raw.githubusercontent.com/acme/kbd/{COMMIT_A}/Sweep%20v2/a%20b.kicad_pcb"
    )
    assert _raw_url("https://github.com/acme/kbd.git", "main", "x") == (
        "https://raw.githubusercontent.com/acme/kbd/main/x"
    )
    with pytest.raises(ValueError):
        _raw_url("not-a-repo", "main", "x")


# ------------------------------------------------------------------ sidecars
def _spec_and_manifest(tmp_path: Path, files: dict[str, bytes]) -> real100.Manifest:
    board = files["pcb/b1.kicad_pcb"]
    side = [
        {"path": p, "git_blob_sha1": git_blob_sha1(d)}
        for p, d in files.items()
        if not p.endswith(".kicad_pcb")
    ]
    entry = _board(
        1, repository="acme/kbd", commit=COMMIT_A, sidecars=side,
        source_size_bytes=len(board), git_blob_sha1=git_blob_sha1(board),
    )  # fmt: skip
    return load_manifest(_manifest(tmp_path, [entry]))


def _serve(monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes]) -> list[str]:
    urls: list[str] = []
    prefix = f"https://raw.githubusercontent.com/acme/kbd/{COMMIT_A}/"

    def fake(url: str, *, timeout: float = 0.0) -> bytes:
        urls.append(url)
        assert url.startswith(prefix)
        return files[url[len(prefix) :]]

    monkeypatch.setattr(real100, "_download", fake)
    return urls


def test_pinned_sidecars_are_verified_and_fetched_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {
        "pcb/b1.kicad_pcb": (BOARDS / "router_basic.kicad_pcb").read_bytes(),
        "pcb/b1.kicad_pro": b'{"meta": {}}',
    }
    m = _spec_and_manifest(tmp_path, files)
    urls = _serve(monkeypatch, files)
    res = fetch_board(m.boards[0], m, tmp_path)
    assert res["status"] == "ok" and res["sidecars"] == ["b1.kicad_pro"]
    assert len(urls) == 2  # no best-effort .kicad_dru probe: exactly the pinned files
    # cached on the second call; prepare copies the pinned sidecar
    assert fetch_board(m.boards[0], m, tmp_path)["cached"] and len(urls) == 2
    prep = prepare_board(m.boards[0], tmp_path)
    assert prep["status"] == "ok" and prep["sidecars"] == ["b1.kicad_pro"]
    assert (tmp_path / "prepared" / "O001" / "b1.kicad_pro").is_file()


def test_sidecar_sha_mismatch_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"pcb/b1.kicad_pcb": b"(kicad_pcb)", "pcb/b1.kicad_pro": b"{}"}
    m = _spec_and_manifest(tmp_path, files)
    _serve(monkeypatch, {**files, "pcb/b1.kicad_pro": b'{"tampered": 1}'})
    with pytest.raises(RuntimeError, match=r"sidecar .* SHA mismatch"):
        fetch_board(m.boards[0], m, tmp_path)
    assert not (tmp_path / "downloads" / "O001" / "b1.kicad_pro").exists()


# ----------------------------------------------------------------------- CLI
def test_cli_manifest_option_and_smoke_selection(tmp_path: Path) -> None:
    assert build_parser().parse_args(["list"]).manifest == real100.default_manifest()
    path = _manifest(tmp_path, [_board(i, repository="a/b", commit=COMMIT_A) for i in range(20)])
    args = build_parser().parse_args(
        ["--manifest", str(path), "--workdir", str(tmp_path / "w"), "run", "--profile", "smoke"]
    )
    assert args.manifest == path and args.workdir == tmp_path / "w"
    m = load_manifest(args.manifest)
    assert [s.id for s in select_specs(m, "smoke")] == [f"O{i:03d}" for i in range(12)]
    assert len(select_specs(m, "standard")) == len(select_specs(m, "full")) == 20
    # Real100 keeps its curated smoke set
    assert [s.id for s in select_specs(load_manifest(), "smoke")] == list(real100.SMOKE_IDS)


def test_list_command_runs_for_an_openboards_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _manifest(tmp_path, [_board(1, repository="a/b", commit=COMMIT_A)])
    assert real100.main(["--manifest", str(path), "--workdir", str(tmp_path), "list"]) == 0
    assert "1 from 1 repositories" in capsys.readouterr().out


# ----------------------------------------------------------- real100_compare
def _ab_args(compare: ModuleType, *extra: str) -> argparse.Namespace:
    args: argparse.Namespace = compare.parse_args(
        ["--baseline", "fixed", "--candidate", "p.json", "--ids", "O001", *extra]
    )
    return args


def test_compare_passes_manifest_before_the_run_subcommand(compare: ModuleType) -> None:
    man = OPENBOARDS / "manifest.json"
    a = _ab_args(compare, "--manifest", str(man))
    assert Path(a.workdir) == OPENBOARDS / "work"  # follows the manifest by default
    cmd = compare.real100_command("fixed", "speed", a, Path("out.jsonl"))
    run = cmd.index("run")
    assert cmd[2:run] == ["--manifest", str(man), "--workdir", str(OPENBOARDS / "work")]
    assert cmd[run + 1 :] == [
        "--profile", "standard", "--modes", "speed", "--no-experience", "--policy", "fixed",
        "--out", "out.jsonl", "--ids", "O001",
    ]  # fmt: skip
    a = _ab_args(compare, "--manifest", str(man), "--workdir", "elsewhere")
    cmd = compare.real100_command("fixed", "speed", a, Path("o.jsonl"))
    assert cmd[cmd.index("--workdir") + 1] == "elsewhere"


def test_compare_without_manifest_is_unchanged(compare: ModuleType) -> None:
    a = _ab_args(compare)
    assert Path(a.workdir) == ROOT / "benchmarks" / "real100" / "work"
    cmd = compare.real100_command("fixed", "accuracy", a, Path("o.jsonl"))
    assert cmd[1].endswith("benchmark_real100.py") and cmd[2] == "run"
    assert "--manifest" not in cmd and "--workdir" not in cmd


def test_compare_finds_prepared_boards_by_manifest_name(
    compare: ModuleType, tmp_path: Path
) -> None:
    folder = tmp_path / "prepared" / "O001"
    folder.mkdir(parents=True)
    (folder / "a.kicad_pcb").write_text("x")
    (folder / "z.kicad_pcb").write_text("x")
    assert compare.prepared_board("O001", tmp_path) == folder / "a.kicad_pcb"
    assert compare.prepared_board("O001", tmp_path, {"O001": "z.kicad_pcb"}) == (
        folder / "z.kicad_pcb"
    )
    assert compare.prepared_board("O002", tmp_path) is None


# ------------------------------------------------------------------- curate.py
MIT_TEXT = (
    "MIT License\n\nCopyright (c) 2023 Jane Maker\n\n"
    "Permission is hereby granted, free of charge, to any person obtaining a copy"
)


def _fake_repo(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes]
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fetch(repo: str, commit: str, path: str) -> bytes | None:
        assert repo == "acme/kbd" and commit == COMMIT_A
        calls.append((commit, path))
        return files.get(path)

    monkeypatch.setattr(curate, "resolve_commit", lambda repo, ref: COMMIT_A)
    monkeypatch.setattr(curate, "fetch_file", fetch)
    return calls


CAND = {"repo": "acme/kbd", "ref": "v1", "board": "hw/kb.kicad_pcb", "domain": "keyboard"}


def _project(board: str = "router_basic") -> dict[str, bytes]:
    return {
        "hw/kb.kicad_pcb": (BOARDS / f"{board}.kicad_pcb").read_bytes(),
        "hw/kb.kicad_pro": (BOARDS / "router_basic.kicad_pro").read_bytes(),
        "LICENSE": MIT_TEXT.encode(),
    }


def test_curate_accepts_a_complete_candidate(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = _project()
    _fake_repo(curate, monkeypatch, files)
    r = curate.evaluate_candidate({**CAND, "tags": ["split"]}, tmp_path)
    assert r["status"] == "accepted", r
    assert r["commit"] == COMMIT_A and r["repository"] == "acme/kbd"
    assert r["git_blob_sha1"] == git_blob_sha1(files["hw/kb.kicad_pcb"])
    assert r["sidecars"] == [
        {
            "path": "hw/kb.kicad_pro",
            "git_blob_sha1": git_blob_sha1(files["hw/kb.kicad_pro"]),
            "size": len(files["hw/kb.kicad_pro"]),
        }
    ]
    assert r["license"] == "MIT" and r["license_file"] == "LICENSE"
    assert r["copyright"] == "Copyright (c) 2023 Jane Maker"
    assert r["attribution"] == f"acme/kbd @ {COMMIT_A}"
    assert isinstance(r["reference_complete"], bool)
    assert r["stats"]["nets_to_route"] > 0 and r["stats"]["copper_layers"] >= 2
    assert r["tags"][:2] == ["split", "keyboard"]
    assert f"{int(r['stats']['copper_layers'])}layer" in r["tags"]
    assert r["difficulty"] in {"small", "normal", "large", "dense", "torture"}

    manifest = curate.build_manifest([r, {"status": "rejected", "repo": "x/y", "board": "b"}])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    m = load_manifest(path)
    (b,) = m.boards
    assert b.id == "O001" and b.repository == "acme/kbd" and b.ref == COMMIT_A
    assert b.sidecars == (("hw/kb.kicad_pro", r["sidecars"][0]["git_blob_sha1"]),)
    rejected = {
        "status": "rejected",
        "repo": "x/y",
        "board": "b",
        "reason": "no licence file found",
    }
    curate.write_corpus_md(manifest, [r, rejected], tmp_path / "C.md")
    text = (tmp_path / "C.md").read_text(encoding="utf-8")
    assert "| O001 |" in text and "no licence file found" in text
    curate.write_attribution_md(manifest, tmp_path / "A.md")
    assert f"acme/kbd @ {COMMIT_A}" in (tmp_path / "A.md").read_text(encoding="utf-8")


def test_curate_rejects_unknown_or_missing_licence(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = {**_project(), "LICENSE": b"All rights reserved. Ask me first."}
    _fake_repo(curate, monkeypatch, files)
    r = curate.evaluate_candidate(CAND, tmp_path / "a")
    assert r["status"] == "rejected" and "unknown licence" in r["reason"]
    del files["LICENSE"]
    r = curate.evaluate_candidate(CAND, tmp_path / "b")
    assert r["status"] == "rejected" and r["reason"] == "no licence file found"
    # a curator override with a note lets it through
    r = curate.evaluate_candidate(
        {**CAND, "license_override": "CERN-OHL-S-2.0", "license_note": "README"}, tmp_path / "c"
    )
    assert r["status"] == "accepted" and r["license"] == "CERN-OHL-S-2.0"


def test_curate_finds_licence_closest_to_the_board(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = {**_project(), "hw/LICENCE": b"Creative Commons Attribution-ShareAlike 4.0"}
    _fake_repo(curate, monkeypatch, files)
    r = curate.evaluate_candidate(CAND, tmp_path)
    assert r["license"] == "CC-BY-SA-4.0" and r["license_file"] == "hw/LICENCE"


def test_curate_rejects_missing_kicad_pro(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = _project()
    del files["hw/kb.kicad_pro"]
    _fake_repo(curate, monkeypatch, files)
    r = curate.evaluate_candidate(CAND, tmp_path)
    assert r["status"] == "rejected" and ".kicad_pro" in r["reason"]


def test_curate_rejects_board_with_nothing_to_route(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_repo(curate, monkeypatch, _project("empty"))
    r = curate.evaluate_candidate(CAND, tmp_path)
    assert r["status"] == "rejected" and r["reason"] == "nothing to route after stripping"


def test_curate_rejects_pre_kicad6_and_missing_boards(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_repo(curate, monkeypatch, _project("kicad5_legacy"))
    r = curate.evaluate_candidate(CAND, tmp_path / "a")
    assert r["status"] == "rejected" and "pre-KiCad-6" in r["reason"]
    _fake_repo(curate, monkeypatch, {})
    r = curate.evaluate_candidate(CAND, tmp_path / "b")
    assert r["status"] == "rejected" and "not found" in r["reason"]


def test_curate_caches_downloads_including_misses(
    curate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_repo(curate, monkeypatch, _project())
    curate.evaluate_candidate(CAND, tmp_path)
    n = len(calls)
    assert n > 0
    curate.evaluate_candidate(CAND, tmp_path)
    assert len(calls) == n


@pytest.mark.parametrize(
    ("text", "spdx"),
    [
        ("CERN Open Hardware Licence Version 2 - Strongly Reciprocal", "CERN-OHL-S-2.0"),
        ("CERN Open Hardware Licence Version 2 - Weakly Reciprocal", "CERN-OHL-W-2.0"),
        ("CERN Open Hardware Licence Version 2 - Permissive", "CERN-OHL-P-2.0"),
        ("Attribution-ShareAlike 4.0 International", "CC-BY-SA-4.0"),
        ("Creative Commons Attribution 4.0 International Public License", "CC-BY-4.0"),
        ("Apache License\n Version 2.0, January 2004", "Apache-2.0"),
        ("Permission is hereby granted, free of charge, to any person", "MIT"),
        ("Redistribution and use in source and binary forms ... Neither the name", "BSD-3-Clause"),
        ("Redistribution and use in source and binary forms, with or without", "BSD-2-Clause"),
        ("GNU GENERAL PUBLIC LICENSE\n Version 3, 29 June 2007", "GPL-3.0"),
        ("GNU GENERAL PUBLIC LICENSE\n Version 2, June 1991", "GPL-2.0"),
        ("SOLDERPAD HARDWARE LICENSE version 2.1", "SHL-2.1"),
        ("This hardware is licensed under CERN-OHL-W-2.0.", "CERN-OHL-W-2.0"),
        ("Attribution-NonCommercial-ShareAlike 4.0 International", None),
        ("All rights reserved.", None),
    ],
)
def test_classify_license(curate: ModuleType, text: str, spdx: str | None) -> None:
    assert curate.classify_license(text)[0] == spdx
