# Known limitations (after the 2026-09-29 stabilization pass)

Also see `docs/stability.md` ("Known limitations (1.x)").

## Routing
- **Dense 4-layer boards are not solved.** `kit-dev-coldfire` (209 nets) still routes only **16/209 nets in a 600 s budget** in both Speed and Accuracy after this pass. `video` (371 nets) was not re-run. The copper that is routed is DRC-clean, and time-outs are reported per net. The cause is not diagnosed yet; it needs its own investigation (per-net grid build on a large 4-layer board, fine-pitch escape, how inner planes are treated). Per-net search in pure Python runs at about 110–150k nodes/s.
  - The time is in raw A* throughput and per-net grid building. On boards that size, Freerouting (Router ▸ Route Board with Freerouting…) is the practical option today.
- **Parallel routing is not implemented.** Nets share board state, so it needs speculative routing in several processes with sequential re-validation. That's the next performance step.
- **Bidirectional A\* is not implemented.** Coarse-to-fine gave a larger, safer gain first.
- **Layer directions are fixed.** They alternate H/V in stack order (first copper layer horizontal) and are not yet read from a board setting.
- **Accuracy is optimal only within a corridor.** It is optimal around the coarse route; the full search runs only when the corridor fails. It is still admissible (weight 1.0) within the corridor.
- **The DRC repair loop is not built.** The bounded route → DRC → classify → rip-up-local → reroute loop with stagnation detection doesn't exist yet. Routed copper is DRC-clean by construction (the exact validator gates every commit), so the loop has had nothing to repair on the measured boards.
- **Through vias only.** Blind/buried intent is not preserved (see `docs/stability.md`).

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
- **Real Intel Iris Xe (dpnp) execution.** No GPU in this container. The GPU path falls back to CPU and says why (`--gpu-check`).
- **KiCad DRC of exported files.** `kicad-cli` is not installed here. The app's internal check and the reload self-check ran on every export.
- **Real KiCad 10 opening the exported board, and a real Freerouting run.** These need Davi's machine.
