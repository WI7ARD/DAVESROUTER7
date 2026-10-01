"""User-initiated export of the experience log (a zip bundle) and reading it back."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import pcbrouter
from pcbrouter.learning.experience import (
    EXPORT_CONTENTS,
    EXPORT_SCHEMA,
    ExperienceLog,
    export_bundle,
    read_bundle,
)
from pcbrouter.learning.policy import router_signature
from pcbrouter.learning.trainer import load_records


def _rec(i: int, board: str, *, schema: int = 2, mode: str = "speed", source: str = "app") -> dict:
    return {
        "schema": f"pcbrouter-experience/{schema}",
        "app": "1.0.0" if schema == 1 else pcbrouter.__version__,
        "source": source,
        "board": board,
        "settings": {"mode": mode},
        "net_f": {"kind": "signal", "pads": 2},
        "outcome": {"status": "SUCCESS", "i": i},
    }


def _log(folder: Path) -> tuple[ExperienceLog, list[dict]]:
    log = ExperienceLog(folder)
    _ = log.salt  # the salt file exists, as in a real installation
    recs = [
        _rec(0, "b1", schema=1, mode="accuracy", source="cli"),
        _rec(1, "b1"),
        _rec(2, "b2", source="real100"),
        _rec(3, "b3", mode="accuracy"),
    ]
    log.append(recs[:2])
    log.append([{"schema": "something-else/9", "board": "zz"}, {"i": 5}])  # unknown: skipped
    log.append(recs[2:])
    with log.path.open("ab") as fh:
        fh.write(b'{"schema": "pcbrouter-experience/2", "board": "torn')  # crash mid-write
    return log, recs


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_export_writes_records_and_a_matching_manifest(tmp_path: Path) -> None:
    log, recs = _log(tmp_path / "exp")
    info = export_bundle(tmp_path / "out.zip", tmp_path / "exp")
    files = _members(tmp_path / "out.zip")
    assert set(files) == {"records.jsonl", "manifest.json"}  # never the salt
    assert log.salt.encode() not in b"".join(files.values())
    lines = files["records.jsonl"].decode().splitlines()
    assert [json.loads(x) for x in lines] == recs  # round trip, torn + unknown skipped
    assert lines[0] == json.dumps(recs[0], sort_keys=True, separators=(",", ":"))
    man = json.loads(files["manifest.json"])
    assert man["schema"] == EXPORT_SCHEMA and man["contents"] == EXPORT_CONTENTS
    assert man["app"] == pcbrouter.__version__ and man["router"] == router_signature()
    assert man["records"] == 4 and man["boards"] == 3 and man["skipped"] == 2
    assert man["by_schema"] == {"pcbrouter-experience/1": 1, "pcbrouter-experience/2": 3}
    assert man["by_app"] == {"1.0.0": 1, pcbrouter.__version__: 3}
    assert man["by_mode"] == {"accuracy": 2, "speed": 2}
    assert man["by_source"] == {"app": 2, "cli": 1, "real100": 1}
    assert man["created"].endswith("+00:00")
    assert info["records"] == 4 and info["boards"] == 3 and not info["empty"]
    assert info["bytes"] == (tmp_path / "out.zip").stat().st_size
    assert info["path"] == str(tmp_path / "out.zip")
    assert not list(tmp_path.glob(".*.tmp"))  # the temporary file is gone


def test_export_reads_keep_rotated_and_current_files(tmp_path: Path) -> None:
    folder = tmp_path / "exp"
    folder.mkdir()
    for name, i in (("experience.keep.jsonl", 0), ("experience.jsonl.1", 1),
                    ("experience.jsonl", 2)):  # fmt: skip
        (folder / name).write_text(json.dumps(_rec(i, f"b{i}")) + "\n", encoding="utf-8")
    export_bundle(tmp_path / "out.zip", folder)
    got = [json.loads(x)["outcome"]["i"] for x in _members(tmp_path / "out.zip")["records.jsonl"]
           .decode().splitlines()]  # fmt: skip
    assert got == [0, 1, 2]


def test_empty_or_missing_log_gives_a_valid_zero_record_bundle(tmp_path: Path) -> None:
    info = export_bundle(tmp_path / "out.zip", tmp_path / "nothing-here")
    assert info["records"] == 0 and info["empty"] and info["boards"] == 0
    assert any("0 records" in w for w in info["warnings"])
    files = _members(tmp_path / "out.zip")
    assert files["records.jsonl"] == b"" and json.loads(files["manifest.json"])["records"] == 0
    assert list(read_bundle(tmp_path / "out.zip")) == []


def test_corrupt_log_does_not_raise(tmp_path: Path) -> None:
    folder = tmp_path / "exp"
    folder.mkdir()
    good = json.dumps(_rec(0, "b"))
    (folder / "experience.jsonl").write_bytes(
        b"\xff\xfe garbage\n[1, 2]\n" + good.encode() + b'\n\x00\x00\n{"schema": \xe2\x82\n'
    )
    (folder / "experience.jsonl.1").write_bytes(b"\x89PNG not json at all")
    info = export_bundle(tmp_path / "out.zip", folder)
    assert info["records"] == 1


def test_refuses_to_overwrite_unless_asked(tmp_path: Path) -> None:
    _log(tmp_path / "exp")
    dest = tmp_path / "out.zip"
    dest.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        export_bundle(dest, tmp_path / "exp")
    assert dest.read_bytes() == b"precious"
    assert export_bundle(dest, tmp_path / "exp", overwrite=True)["records"] == 4
    assert zipfile.is_zipfile(dest)


def test_unwritable_destination_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        export_bundle(tmp_path / "missing-dir" / "out.zip", tmp_path / "exp")


def test_trainer_reads_bundles_and_folders_together(tmp_path: Path) -> None:
    _, recs = _log(tmp_path / "shared")
    export_bundle(tmp_path / "shared.zip", tmp_path / "shared")
    mine = ExperienceLog(tmp_path / "mine")
    mine.append([_rec(9, "m1", source="fixture")])
    got = load_records([tmp_path / "shared.zip", tmp_path / "mine"])
    assert got == [*recs, _rec(9, "m1", source="fixture")]
    assert [r["source"] for r in got] == ["cli", "app", "real100", "app", "fixture"]


def test_trainer_refuses_a_foreign_zip(tmp_path: Path) -> None:
    bad = tmp_path / "other.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"schema": "someone-else/1"}))
        zf.writestr("records.jsonl", "")
    with pytest.raises(ValueError, match="unsupported export schema"):
        load_records([bad])
    nozip = tmp_path / "fake.zip"
    nozip.write_text("not a zip")
    with pytest.raises(ValueError, match="not a zip"):
        load_records([nozip])


def test_cli_export_experience(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from pcbrouter.app.application import main
    from pcbrouter.learning.experience import default_dir

    _log(default_dir())  # the isolated app data folder (tests/conftest.py)
    out = tmp_path / "data.zip"
    assert main(["--export-experience", str(out)]) == 0
    text = capsys.readouterr().out
    assert "records: 4" in text and "boards: 3" in text and str(out) in text
    assert "nothing leaves the computer unless you export it" in text
    assert json.loads(_members(out)["manifest.json"])["records"] == 4
    assert main(["--export-experience", str(out)]) == 2  # exists: refused
    assert "--overwrite" in capsys.readouterr().out
    assert main(["--export-experience", str(out), "--overwrite"]) == 0
    assert main(["--export-experience", str(tmp_path / "no" / "such" / "dir.zip")]) == 2
    assert "Could not write" in capsys.readouterr().out
