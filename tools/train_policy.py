"""Train a routing policy (learning level 2) from experience logs.

python tools/train_policy.py --log benchmarks/real100/work/experience --out policy.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pcbrouter.learning.trainer import main

if __name__ == "__main__":
    raise SystemExit(main())
