"""Convenience launcher for the DAVESROUTER Real100 benchmark corpus."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pcbrouter.benchmark.real100 import main

if __name__ == "__main__":
    raise SystemExit(main())
