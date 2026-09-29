# Audit before this fix (branch at `origin/main` 4c73e31, 2026-09-29)

## Environment
- **Python:** 3.12 (`requires-python >=3.12`; 3.11 is refused by pip, as intended).
- **Clean install:** `python3.12 -m venv v && v/bin/pip install -e .` → `pcbrouter --version` works, and GUI/router/worker modules import.
- **Mandatory dependencies:** PySide6, pydantic, numpy.
- **Optional dependencies:** `gpu-intel` (dpnp), `gpu-cuda`, `ai`/`openai`/`anthropic`, `packaging`, `dev`.
- **Entry point:** `pcbrouter = pcbrouter.app.application:main` (GUI unless `--inspect`, `--freeroute`, … are given).

## Architecture (already in place before this pass)
- **Worker process:** routing runs in a persistent worker process, with progress, cancel and watchdogs (see `ROUTING_PIPELINE.md`).
- **Validation:** an exact validator gates every commit.
- **Export:** append-only with a reload self-check.
- **Search limits:** every search is bounded (node and time limits, board budget, pass and rip-up limits).

## What actually happened (reproduced, CPU only)
Boards used:
- **Real KiCad demo boards:** tracks and vias stripped so there is routing work. They are local only and never committed (GPL):
  - `pic_programmer` (KiCad 10, 2 layers, 33 nets);
  - `complex_hierarchy` (2 layers, 49 nets);
  - `kit-dev-coldfire` (4 layers, 209 nets);
  - `video` (4 layers, 371 nets).
- **Davi's `Router_Benchmark_RevA`:** 2 layers, 32 nets, real `.kicad_pro`/`.kicad_dru`.

| Board | Result on `origin/main` |
|---|---|
| pic_programmer | 33/33 but **167.6 s**, 531 MB (single nets 1.9M nodes / 42 s) |
| complex_hierarchy | 49/49, 65.1 s, 509 MB |
| kit-dev-coldfire | **18/209** in the 600 s budget, reported **CANCELLED** (nobody cancelled), 174 nets with no reason |
| video | **17/371** in 600 s, reported **CANCELLED** |
| Router_Benchmark_RevA, Speed | **21/32** in 170 s (11 nets NO_PATH / node limit) |
| Router_Benchmark_RevA, Accuracy | **15/32** when the 600 s budget ran out |
| Any board without `.kicad_pro` | every net RULE_UNKNOWN in < 1 s, with guidance text (correct: no guessed rules) |

## Root causes found
1. **The A\* heuristic collapses on spread-out nets.**
   - It measured distance to *one* bounding box around all unconnected pads per layer.
   - On a net spread over the board that box covers most of it, so the heuristic is ≈0 almost everywhere and A* becomes a Dijkstra flood.
   - Proven on `pic_programmer` `/DATA-RB7`: a plain flood fill reaches every target, yet A* stopped at its node limit.
2. **No layer-direction discipline on 2-layer boards.** The benchmark is built from crossing connections. Without H/V preference, early routes wall off both layers and later nets find no path.
3. **Budget exhaustion was reported as CANCELLED.** Nets never reached had an empty reason.
4. **Partial routes lost their reason.** They were always labelled NO_PATH with an empty message.
5. **The GUI thread rasterised congestion maps for the AI panel.** The label was rebuilt after every board load and accept, even when AI was unused: measured stalls of 300–580 ms.
6. **Slow live-preview repaint.** The preview used a dashed pen over hundreds of track shapes.

The dense 4-layer boards (coldfire, video) are still limited by raw per-net search cost. See `KNOWN_LIMITATIONS.md`.
