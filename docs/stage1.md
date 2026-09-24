# Stage 1 — Foundation + KiCad Board Inspector

Version `0.1.0-stage1`. Status: **complete** (see the acceptance checklist below).

Stage 1 is a **read-only** inspector. It performs **no autorouting** and **no AI API calls**,
and never writes to a board file.

## Scope delivered

| Area | Delivered |
|---|---|
| Domain model | `Board`, `BoardOutline`, `Layer`, `Net`, `Component`, `Footprint`, `Pad`, `Track` (straight + arc), `Via` (through/blind/micro), `Point`, `BoundingBox`, `DesignRules`; immutable; integer nanometres |
| KiCad loading | KiCad 5–9 formats (plus best-effort for newer), outline (lines, arcs incl. KiCad 5 centre/angle form, circles, rects, polygons), copper layers, footprints, reference/value/properties, pads (position, size, shape, type, drill, layers, net), nets (by code or by name), tracks, arcs, vias, KiCad-5-style board rules, board thickness |
| Error handling | `KiCadLoadError` hierarchy with user messages; per-item skip-and-report; line numbers |
| Viewer | Pan (middle / Space+left), wheel zoom at cursor, zoom-to-fit, adaptive 1-2-5 grid, board substrate fill, layer colours, holes, back-side footprints dashed, LOD labels, hover tooltips, click selection, coordinate readout |
| Panels | Project (facts, stats, timings, load notes, workspace, components), Layers (visibility, active layer, overlays), Nets (search, sort, highlight, hide, isolate, show all), Inspector, Log |
| Menus / toolbar / status bar | As specified; Router and AI entries are labelled "Available in a later stage" and explain themselves when used |
| Settings | JSON, atomic writes, corrupt-file quarantine, dialog with General / Viewer / Compute / AI Providers / Routing / GPU tabs (last three inactive and labelled) |
| Compute | `ComputeBackend` ABC, `CPUBackend`, placeholder `GPUBackend`, non-raising GPU detection (background thread in the GUI) |
| AI | `AIProvider` interface, provider/context policy models, strict `PCBCommand` schema with board-aware validation |
| Commands | `CommandBus` with read-only enforcement; open/close/summary/validate-AI commands; shared by GUI and CLI |
| History | Undo/redo manager, metadata snapshot store, route-proposal state machine |
| Logging | Console + rotating file, startup/env/load/timing/count/compute/shutdown events, secret redaction |

## Internal units

One canonical unit: **integer nanometres** (`pcbrouter.domain.units.Nm`). KiCad stores
geometry in integer nanometres and files carry at most 6 decimals of mm, so
`parse_mm("12.345678") == 12_345_678` exactly. Conversions exist only in
`domain/units.py` (`mm_to_internal`, `internal_to_mm`, `parse_mm`, `format_mm`). The canvas
converts to millimetre floats in a single helper at the display boundary.

## KiCad parsing strategy

Own zero-dependency S-expression parser → adapter that interprets KiCad semantics → domain
model. No third-party KiCad objects exist anywhere in the application. Rationale and details:
[architecture.md §4](architecture.md#4-kicad-adapter-pcbrouterkicad).

Besides the bundled fixtures, the loader was checked (manually, not committed — those
files are GPL-licensed KiCad demos) against four real KiCad-generated boards:

| Board | Format | Result |
|---|---|---|
| `complex_hierarchy` | KiCad 9 | 68 footprints, 165 pads, 364 tracks; no warnings |
| `kit-dev-coldfire-xilinx_5213` | KiCad 9 | 160 footprints, 825 pads, 2 935 tracks, 253 vias, 4 layers; no warnings |
| `pic_programmer` | **KiCad 10** (20260206) | 63 footprints, 247 pads, 370 tracks, 6 vias; loads with the "newer than tested" warning, as designed |
| `video` | KiCad 9 | 189 footprints, 2 118 pads, 7 932 tracks, 808 vias, 4 layers; scene of 11 237 items built in 0.25 s |

## Performance notes

- Parse time is recorded for every load (Project panel, log, CLI). Measured on the 5.8 MB
  `video` board: ≈0.9 s parse, ≈0.23 s model build, ≈0.25 s scene build.
  Node allocation dominates parsing (a tokenizer rewrite gained only ~10 %), so no
  premature optimisation was applied; a compiled tokenizer is an option if profiling of
  larger boards later demands it.
- The scene is never rebuilt after load. Picking a 1 600-footprint synthetic board costs
  well under a millisecond per mouse move (test asserts < 20 ms).

## Tests

Run: `pytest` (headless; GUI tests use `QT_QPA_PLATFORM=offscreen`).

**241 tests, all passing** — 182 unit + 59 integration, deterministic, order-independent
(verified in both orders), no dependency on private boards.

| Suite | Covers |
|---|---|
| `unit/test_units.py` | exact mm↔nm conversion, rounding, garbage/inf/bool rejection |
| `unit/test_geometry.py` | rotation convention, bounding boxes, arcs (3-point and KiCad 5 form) |
| `unit/test_domain_models.py`, `test_board_bounds.py` | layers, items, statistics, index, immutability, outline loops, bounds |
| `unit/test_sexpr_parser.py` | syntax, escapes, 9 malformed-input cases with line/column, depth guard |
| `unit/test_kicad_adapter.py` | every supported construct, versions, nets by name, bad-item skipping, duplicates |
| `unit/test_loader.py` | missing file, wrong extensions, directories, permission errors, binary garbage, BOM, hashing |
| `unit/test_command_schema.py` | spec example, 19 invalid commands, schema export, board-aware validation |
| `unit/test_compute.py` | CPU backend, GPU detection matrix via fakes, sysfs parsing, fallback |
| `unit/test_settings.py` | defaults, round-trip, quarantine of 5 corrupt variants, recent list, no secret fields |
| `unit/test_logging.py` | redaction patterns, file+console handlers, banner, unwritable log dir |
| `unit/test_history.py`, `test_command_bus.py`, `test_project_manager.py` | undo/redo, snapshots, proposals, bus policy, read-only rejection, workspace paths |
| `integration/test_load_fixtures.py` | all fixtures with hand-computed expected values; large synthetic board |
| `integration/test_read_only.py` | **SHA-256, size and mtime identical** after open→inspect→close for every readable fixture; CLI too |
| `integration/test_ui.py` | real widgets: open, zoom, pan (middle and Space+drag), layer toggles, active layer, net highlight/isolate/hide, click-to-inspect pad/track/via/component, hover, errors, placeholders, shortcuts, settings persistence across restart, large-board interactivity |
| `integration/test_optional_dependencies.py` | app and CLI run with KiCad/CUDA/OpenAI/Anthropic/keyring imports **blocked**; real GUI entry point starts, loads, shuts down cleanly |
| `integration/test_cli.py` | `--version`, `--inspect`, `--check-command`, exit codes, log file content |

Quality gates (all clean): `black --check`, `ruff check` (incl. naming, bugbear, pyupgrade),
`mypy --strict` over `src/` **and** over `tests/` + `tools/`.

## Known limitations

- Zones, text, dimensions, silkscreen/fab graphics and board-edge graphics *inside
  footprints* are not drawn (counted and reported as "not displayed").
- Pad shapes `trapezoid` and `custom` are drawn as rectangles (the inspector says so);
  chamfered corners are not drawn.
- Footprint bodies are drawn as the rotated courtyard (or fab, or pad) bounding rectangle,
  not the real outline.
- Design rules: only KiCad 5-style rules stored in the board file are read; KiCad 6+ rules
  in `.kicad_pro`/`.kicad_dru` show as "unknown".
- Items with an unresolvable net reference are skipped with a warning rather than shown
  without a net.
- Large files load on the GUI thread (a wait cursor is shown); background loading with a
  progress bar is planned.
- When the window is narrow, the transient status-bar message is truncated by the
  permanent fields (the message is also in the log panel).
- GUI verified headless (offscreen Qt) on Linux, plus screenshots. Not yet run on a
  physical Windows 11 machine; Windows-specific code paths (APPDATA paths, registry CPU
  name, PowerShell GPU query) are written defensively but untested on real Windows.

## Intentional Stage 2+ placeholders

| Placeholder | Where | Behaviour |
|---|---|---|
| Route Selected Net / Route Board | Router menu | Labelled "Available in a later stage"; dialog explains; nothing happens to the board |
| Configure AI Providers | AI menu, Settings ▸ AI Providers | Same; provider checkboxes disabled |
| Routing, GPU settings tabs | Settings | Informational banners only |
| GPU backend option | Settings ▸ Compute | Disabled entry; `GPUBackend.initialize()` raises `BackendUnavailableError` |
| `AIProvider` | `ai/provider.py` | Interface only, no implementations |
| `HistoryManager`, `SnapshotStore`, `RouteProposal` | `history/` | Exercised with metadata-only actions |
| Workspace directories | `project/workspace.py` | Paths computed, nothing created |
| `modifies_board` commands | `CommandBus` | Rejected with "Board modification is disabled…" |

## Acceptance checklist

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Install the project | PASS | `pip install -e ".[dev]"`; wheel built and installed into a clean venv, `pcbrouter` entry point works |
| 2 | Start the desktop application | PASS | `test_gui_entry_point_starts_and_exits_cleanly` runs the real `main()` (offscreen) |
| 3 | Open a `.kicad_pcb` | PASS | `test_open_board_populates_every_panel`, real KiCad 9/10 boards |
| 4 | See the board | PASS | render-count assertions + screenshots |
| 5 | Zoom and pan | PASS | `test_zoom_and_pan`, `test_space_left_drag_pans` |
| 6 | Toggle PCB layers | PASS | `test_layer_visibility_toggle` |
| 7 | Inspect footprints | PASS | `test_click_to_inspect_pad_track_via_component` (U1) |
| 8 | Inspect pads | PASS | same test (J1.3) |
| 9 | Inspect traces | PASS | same test (In2.Cu track) |
| 10 | Inspect vias | PASS | same test |
| 11 | Browse nets | PASS | `test_nets_highlight_isolate_hide` (filter, sort model) |
| 12 | Highlight a selected net | PASS | same test; dimming asserted |
| 13 | View board statistics | PASS | Project panel + status bar (`test_open_board_populates_every_panel`) |
| 14 | View CPU information | PASS | `test_about_and_compute_info_text` |
| 15 | See whether a GPU is detected | PASS | status bar + dialog; `test_gpu_detection_updates_status_bar`, detection unit tests |
| 16 | Open settings | PASS | `test_settings_dialog` |
| 17 | Restart and retain settings | PASS | `test_settings_persist_across_restart` (two windows), GUI entry-point test |
| 18 | Close the PCB without modifying it | PASS | `test_close_board_is_read_only`, `test_read_only.py` (SHA-256 identical) |
| 19 | Run pytest successfully | PASS | 241 passed |
| — | Works without KiCad / CUDA / OpenAI / Anthropic | PASS | `test_optional_dependencies.py` (imports blocked) |
| — | Windows 11 | PARTIAL | Cross-platform code paths, not executed on real Windows yet |
