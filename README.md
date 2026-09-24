# AI PCB Router

A desktop PCB engineering application for **KiCad** boards, built toward
deterministic, AI-assisted autorouting.

**Current version: `0.1.0-stage1` — Stage 1 of 10: Foundation + KiCad Board Inspector.**

> **Stage 1 does not perform autorouting or AI API calls.**
> It is a read-only inspector. It never modifies your `.kicad_pcb` files.

---

## What Stage 1 does

- Loads `.kicad_pcb` files (KiCad 5 → 9 formats; newer files load best-effort with a warning)
  **read-only**, verifying the file's SHA-256 is unchanged when the board is closed.
- Translates the board into an internal domain model (integer nanometres) and shows:
  board outline, footprints, pads (SMD/THT/NPTH, rotated), straight and arc tracks,
  through/blind/micro vias, on any number of copper layers.
- 2D viewer: pan (middle mouse or Space + drag), wheel zoom, zoom-to-fit, adaptive grid,
  cursor coordinates, hover tooltips, click-to-select.
- **Inspector** for components, pads, tracks, vias and nets. Values the file does not state
  are shown as *unknown*, never invented.
- **Nets panel**: searchable table (pads, tracks, vias, routed length), highlight, hide,
  isolate, show all.
- **Layers panel**: per-layer visibility, active layer, footprint/label overlays.
- **Project panel**: file facts, statistics, parse timing, load notes (anything skipped or
  not displayed is reported), component list.
- CPU information and GPU detection (NVIDIA/CUDA, other vendors, missing libraries).
- Persistent settings (theme, grid, recent boards, window layout, panels).
- Structured logging to console + rotating file, with secret redaction.
- A headless CLI that uses the same command bus as the GUI:
  `pcbrouter board.kicad_pcb --inspect`.

## What is NOT implemented (by design, yet)

| Feature | Status |
|---|---|
| Autorouting (net / board), rip-up & reroute, optimisation | Later stage — menu items say "Available in a later stage" |
| OpenAI / Anthropic / OpenAI-compatible / local LLM calls | Later stage — interfaces and command schema only |
| Saving / exporting KiCad files, undo/redo of edits | Stage 9 — `Ctrl+S` is intentionally unmapped |
| GPU acceleration | Later stage — detection only |
| Zones, text, dimensions, silkscreen graphics | Parsed/counted and reported as "not displayed" |
| Design rules from `.kicad_pro` / `.kicad_dru` | Later stage — only rules stated in the board file are read |

See [docs/stage1.md](docs/stage1.md) for exact limitations and
[docs/roadmap.md](docs/roadmap.md) for the plan.

## Supported platforms

- **Windows 11** and **modern Linux** (x86-64). macOS is untested.
- **Python 3.12+**.
- KiCad does **not** need to be installed. CUDA, OpenAI and Anthropic packages are **not**
  needed (the test suite proves the app runs with all of them blocked).

## Setup

```bash
git clone <this repo> ai-pcb-router
cd ai-pcb-router
python3.12 -m venv .venv
# Linux:   source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

On minimal Linux installs Qt needs a few system libraries, e.g. on Ubuntu/Debian:
`sudo apt install libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3`.

## Run

```bash
pcbrouter                              # start the desktop app
pcbrouter path/to/board.kicad_pcb      # start and open a board
python -m pcbrouter                    # same, without the console script

pcbrouter board.kicad_pcb --inspect    # headless JSON summary (no GUI)
pcbrouter board.kicad_pcb --check-command '{"operation":"route_net","target":"GND"}'
pcbrouter --version
```

Try it on the bundled fixtures, e.g. `pcbrouter tests/fixtures/boards/four_layer.kicad_pcb`.
For a large synthetic board: `python tools/generate_synthetic_board.py big.kicad_pcb 60 60`.

### Keyboard shortcuts

| Keys | Action |
|---|---|
| `Ctrl+O` | Open PCB |
| `Ctrl+W` | Close PCB |
| `F` | Zoom to fit |
| `G` | Toggle grid |
| `Ctrl+,` | Settings |
| `Ctrl+=` / `Ctrl+-` | Zoom in / out |
| Mouse wheel | Zoom at cursor |
| Middle drag, or `Space` + left drag | Pan |
| `Ctrl+Q` | Exit |

### Where files go

| What | Linux | Windows |
|---|---|---|
| Settings | `~/.config/ai-pcb-router/settings.json` | `%APPDATA%\AI PCB Router\settings.json` |
| Logs | `~/.local/state/ai-pcb-router/logs/` | `%LOCALAPPDATA%\AI PCB Router\logs\` |
| Future workspaces | `~/.local/share/ai-pcb-router/workspaces/` | `%LOCALAPPDATA%\AI PCB Router\workspaces\` |

Override with `PCBROUTER_CONFIG_DIR`, `PCBROUTER_DATA_DIR`, `PCBROUTER_LOG_DIR`.
Nothing is ever written next to your KiCad project.

## Test

```bash
pytest                 # full suite (GUI tests run headless via QT_QPA_PLATFORM=offscreen)
black --check src tests tools
ruff check src tests tools
mypy                   # strict mode, configured in pyproject.toml
```

## Architecture (short version)

```
 GUI (PySide6)   CLI   (future: AI pipeline, scripting API)
        \         |         /
         +--> CommandBus --+        read-only policy, logging, timing, history
                  |
     ProjectManager / HistoryManager / ComputeManager
                  |
          Domain model (immutable, integer nm)   <--   KiCad adapter (read-only)
```

- The **domain model** (`pcbrouter.domain`) is ours; no KiCad or third-party objects leak
  past the adapter (`pcbrouter.kicad`).
- **The LLM must never directly modify board geometry.** Future AI output is only accepted as
  a strictly validated structured command (`pcbrouter.ai.command_schema`) that the
  deterministic router executes.

Full details: [docs/architecture.md](docs/architecture.md).

## Security rules (in force from Stage 1)

- Never store plaintext API keys in project or settings files (future: OS keyring only).
- Never embed API keys into PCB files.
- Never send PCB files to an AI provider automatically.
- Network operations require deliberate provider configuration.
- Prompts will carry summarised/selected engineering context unless the user explicitly
  authorises more.
- Never execute code returned by an AI model.
- Never log secrets (enforced additionally by a redaction filter on every log handler).

## Roadmap

1. **Foundation + KiCad board inspector** ← *you are here*
2. Board model completion (zones, keepouts, net classes, project rules, connectivity/ratsnest)
3. Rule resolution + DRC engine
4. CPU router core: single net, multi-layer, vias; route proposals with preview
5. AI provider integration → validated structured commands
6. Board-level routing, net ordering, rip-up & reroute, optimisation
7. Advanced constraints: differential pairs, length matching, critical nets
8. GPU acceleration backend
9. Safe KiCad writing: save/export, snapshots, before/after diff, accept/reject, undo/redo
10. Productisation: installers, performance, plugin/scripting API

## Screenshots

![Stage 1 inspector showing a four-layer test board](docs/images/stage1-inspector.png)

*More screenshots will be added as features land.*

## License

MIT — see [LICENSE](LICENSE).
