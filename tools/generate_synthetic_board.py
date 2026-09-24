"""Write a large synthetic board for manual GUI performance testing.

Usage: python tools/generate_synthetic_board.py OUTPUT.kicad_pcb [COLUMNS ROWS]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.synthetic import generate_board


def main() -> int:
    if len(sys.argv) not in (2, 4):
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    cols, rows = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) == 4 else (60, 60)
    if out.exists():
        print(f"refusing to overwrite existing file: {out}")
        return 1
    text, counts = generate_board(cols, rows)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
