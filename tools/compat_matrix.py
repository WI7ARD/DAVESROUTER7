"""Render docs/KICAD_COMPATIBILITY.md from src/pcbrouter/rules/compat.json.

Usage: python tools/compat_matrix.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pcbrouter" / "rules" / "compat.json"
DOC = ROOT / "docs" / "KICAD_COMPATIBILITY.md"


def render() -> str:
    data = json.loads(SRC.read_text(encoding="utf-8"))
    out = [
        "# KiCad compatibility matrix",
        "",
        "Generated from `src/pcbrouter/rules/compat.json` by `tools/compat_matrix.py`;",
        "`tests/unit/test_compat_matrix.py` keeps it consistent with the rule engine.",
        f"Reference: {data['kicad_reference']}.",
        "",
        "| State | Meaning |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in data["states"].items()],
        "",
    ]
    for kind in ("property", "function", "constraint", "board"):
        rows = [r for r in data["features"] if r["kind"] == kind]
        out += [f"## {kind.capitalize()}{'s' if kind != 'board' else ' features'}", ""]
        out += ["| Feature | Status | Notes | Evidence |", "|---|---|---|---|"]
        out += [
            f"| `{r['feature']}` | **{r['status']}** | {r.get('notes', '')} | "
            f"{r.get('evidence', '')} |"
            for r in rows
        ]
        out.append("")
    return "\n".join(out)


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        current = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        if current != text:
            print(f"{DOC} is out of date: run python tools/compat_matrix.py")
            return 1
        return 0
    DOC.write_text(text, encoding="utf-8")
    print(DOC)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
