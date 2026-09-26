"""Stage 3 geometry-engine benchmark (hardware dependent; numbers are not tests).

Usage: python tools/bench_geometry.py [BOARD.kicad_pcb | COLUMNS ROWS]

Without arguments a synthetic 40x40 board (tests.fixtures.synthetic) is generated
in a temporary directory. Reports object count, board load, geometry + index
build, single and 1,000 segment checks, occupancy generation and internal DRC.
"""

from __future__ import annotations

import platform
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.geometry import Point
from pcbrouter.domain.units import mm_to_internal
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules


def _board_path(args: list[str], tmp: Path) -> Path:
    if len(args) == 1:
        return Path(args[0])
    from tests.fixtures.synthetic import generate_board

    cols, rows = (int(args[0]), int(args[1])) if len(args) == 2 else (40, 40)
    text, _ = generate_board(cols, rows)
    path = tmp / f"synthetic_{cols}x{rows}.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _board_path(argv, Path(tmpdir))
        t0 = time.perf_counter()
        loaded = load_board(path)
        t_load = time.perf_counter() - t0
        engine = BoardEngine(loaded.board, load_project_rules(path))
        t0 = time.perf_counter()
        geo = engine.geometry
        t_geo = time.perf_counter() - t0
        _ = engine.resolver
        box = loaded.board.bounds
        assert box is not None
        layer = geo.copper_layers[0]
        rnd = random.Random(1)
        reach = mm_to_internal(2)  # router-like local segments (<= 2 mm)
        pts = []
        for _ in range(1000):
            a = Point(rnd.randint(box.min_x, box.max_x), rnd.randint(box.min_y, box.max_y))
            pts.append(
                (a, Point(a.x + rnd.randint(-reach, reach), a.y + rnd.randint(-reach, reach)))
            )
        width = mm_to_internal(0.2)
        t0 = time.perf_counter()
        engine.validator.validate_segment(None, layer, *pts[0], width)
        t_one = time.perf_counter() - t0
        t0 = time.perf_counter()
        for a, b in pts:
            engine.validator.validate_segment(None, layer, a, b, width)
        t_many = time.perf_counter() - t0
        t0 = time.perf_counter()
        occ = engine.occupancy(layer, None, width, mm_to_internal(0.1))
        t_occ = time.perf_counter() - t0
        t0 = time.perf_counter()
        drc = engine.run_drc()
        t_drc = time.perf_counter() - t0
    print(
        f"machine: {platform.processor() or platform.machine()} / "
        f"Python {platform.python_version()}"
    )
    print(f"board: {path.name}  copper objects: {len(geo.copper):,}  holes: {len(geo.holes):,}")
    print(f"board load:            {t_load * 1e3:9.1f} ms")
    print(f"geometry + index:      {t_geo * 1e3:9.1f} ms  (index {geo.index_seconds * 1e3:.1f} ms)")
    print(f"single segment check:  {t_one * 1e3:9.3f} ms")
    print(f"1,000 local checks:    {t_many * 1e3:9.1f} ms  ({t_many:.3f} ms avg)")
    print(f"occupancy 0.1 mm grid: {t_occ * 1e3:9.1f} ms  ({occ.spec.cell_count:,} cells)")
    print(f"internal DRC:          {t_drc * 1e3:9.1f} ms  ({drc.check_count:,} checks, "
          f"{len(drc.errors)} errors)")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
