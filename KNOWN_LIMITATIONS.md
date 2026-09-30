# Known limitations (v1.1.1)

Also see `docs/stability.md` ("Known limitations (1.x)").

## Routing
- **Dense 4-layer boards are only partly routed.** `kit-dev-coldfire` (4 layers, 209 nets) with a 600 s budget: Speed + Auto parallel (3 helpers) **150/209**, Speed single worker **135/209**, Accuracy single worker **169/209**. That's up from 16/209 at the start of v1.1.1 work. All routed copper is DRC-clean. The remaining failures are:
  - *no legal path* at 0.5 mm-pitch QFP escapes (the pad rows are boxed in on the routing grid);
  - congested areas;
  - partial power nets (GND, +3.3V) that hit the per-net time limit.

  `video` (371 nets) was not re-run.
- **Parallel runs are not bit-for-bit repeatable.** Results are committed in the order helpers finish, so geometry can differ slightly between runs; every result still passes the exact validator. *Single worker* is deterministic.
  - Parallel routing needs 12+ nets and the CPU backend. Helpers take a few seconds to start (a copy of the board per helper), so small boards don't benefit.
  - Nets whose regions overlap (e.g. board-wide power nets) run one at a time.
- **Per-net grid building is expensive on big boards:** about 40 % of routing time on coldfire (`grid_s` 245 s of 600 s over 3 helpers).
- **Bidirectional A\* is not implemented.** Coarse-to-fine gave a larger, safer gain first.
- **Layer directions are fixed.** They alternate H/V in stack order (first copper layer horizontal) and are not read from a board setting.
- **Accuracy is optimal only within a corridor.** It is optimal around the coarse route; the full search runs only when the corridor fails.
- **The DRC repair loop is not built.** Routed copper is DRC-clean by construction (the exact validator gates every commit), so no route → DRC → rip-up loop exists.
- **Through vias only.** Blind/buried vias are not generated, and routing is tested only on 2- and 4-layer boards. Boards with more copper layers load, but routing them is untested.
- **Straight segments only** (0/45/90°). No arcs.

## GPU (Intel, optional)
- **Not yet measured on a real Iris Xe with the new kernel.** The development machine has no GPU. The fused relaxation kernel was verified on the OpenCL CPU device (SYCL runtime):
  - bit-identical to the NumPy reference and exactly optimal against Dijkstra;
  - whole boards route through it with 0 validator errors.

  On that CPU device it is 2–3.5× slower than the CPU A* (e.g. Router_Benchmark_RevA: 288 s vs 83 s, 32/32 both). The earlier array version measured 17–236× slower than A* on an Iris Xe.
- **Auto therefore stays on the CPU** for Intel GPUs until `tools/bench_gpu.py` numbers from a real GPU show where the kernel wins.
- **No bend costs in GPU paths** (the state is (layer, cell), with no direction). Via-count limits force the CPU A*.
- **Obstacle-grid building is still CPU work**, even though it is 40–50 % of the time on fine-pitch 4-layer boards (see docs/PROFILE.md).
- **The routing kernel is built at run time** from OpenCL C. A driver that cannot build it leaves routing on the CPU; `--gpu-check` stage `fused_kernel` says so.

## Rules
- **No project file means no routing.** Without a `.kicad_pro` / `.kicad_dru`, rules are unknown and every net reports RULE_UNKNOWN with guidance. KiCad's built-in defaults are deliberately not guessed.
- **Only the router's rules are checked.** The demo boards used for testing had no project file, so the tests added a generic one. Their stored zone fills then report pre-existing hole or edge errors (4 on `pic_programmer`, 1 on `complex_hierarchy`) that are unrelated to routing. KiCad refills zones; our checker does not.

## GUI
- **Short pauses remain,** measured under Xvfb software rendering:
  - 100–370 ms when a finished routing result is shown (preview and result table);
  - about 150–300 ms when accepting about 400 objects (validated commit on the GUI thread);
  - about 200–350 ms when the Internal Geometry Check result is drawn.

  There are no stalls over 100 ms while routing is running.
- **Opening a board blocks the GUI thread.** It is synchronous: 54–381 ms for the tested boards, about 1 s for a 4.4 MB board.
- **Cancel can take a moment.** It is fast (≈0.24 s) during routing. During board preparation it waits for the current step (≤ about 2 s on the tested boards). A hung worker is terminated after 5 s.

## Not verified here
- **Real Intel Iris Xe execution.** No GPU in this container. On Davi's machine, run `pcbrouter.exe --gpu-check` (installed app) and `python tools/bench_gpu.py <boards> --report gpu_report.json` (source).
- **KiCad DRC of exported files.** `kicad-cli` is not installed here. The app's internal check and the reload self-check ran on every export.
- **Real KiCad 10 opening the exported board.** This needs Davi's machine.
