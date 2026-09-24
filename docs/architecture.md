# Architecture

This document describes the Stage 1 architecture and the boundaries that later stages
must respect. The overriding principle:

> **The LLM MUST NOT directly modify board geometry.**
> AI output is data to be validated, never an instruction to be executed.

## 1. Layer diagram

```
+-----------------------------------------------------------------------------------+
|  PRESENTATION                                                                     |
|   ui/ (PySide6)                          app/cli.py (headless)                    |
|   MainWindow, PcbCanvas, panels          --inspect, --check-command               |
+--------------------------------+--------------------------------------------------+
                                 | commands only (never parsers or router internals)
                                 v
+-----------------------------------------------------------------------------------+
|  APPLICATION                                                                      |
|   commands/CommandBus  --->  read-only policy, validation, timing, logging        |
|        |                                                                          |
|        +--> project/ProjectManager   (open board, SHA-256, workspace paths)       |
|        +--> history/HistoryManager   (undo/redo, snapshots, proposals: groundwork)|
|        +--> compute/ComputeManager   (CPU backend now, GPU placeholder)           |
+--------------------------------+--------------------------------------------------+
                                 |
                                 v
+-----------------------------------------------------------------------------------+
|  DOMAIN  (pure Python, no Qt, no KiCad objects)                                   |
|   Board, Layer, Net, Component, Footprint, Pad, Track, Via, BoardOutline,         |
|   Point, BoundingBox, DesignRules          -- immutable, integer nanometres       |
+--------------------------------+--------------------------------------------------+
                                 ^ builds
+--------------------------------+--------------------------------------------------+
|  ADAPTERS                                                                         |
|   kicad/  parser.py (S-expr) -> adapter.py (semantics) -> loader.py (read-only)   |
|   ai/     provider.py (interface only)  command_schema.py (strict Pydantic)       |
|   settings/, app_logging/, utils/paths.py                                         |
+-----------------------------------------------------------------------------------+

Future AI flow (Stage 5+):

 User prompt --> AIProvider --> raw text --> parse_command() --> validate_against_board()
      --> CommandBus --> deterministic planner --> CPU/GPU router --> geometry check
      --> DRC --> RouteProposal preview/diff --> user ACCEPT --> KiCad writer (Stage 9)
```

Dependency rule: arrows point downwards only. `domain` imports nothing from the rest of the
application; only `ui` and `app` import Qt.

## 2. UI layer (`pcbrouter.ui`)

- `MainWindow` is glue: it translates gestures into commands on the `CommandBus` and routes
  results to panels. It never parses files and will never call router internals.
- `PcbCanvas` (QGraphicsView) builds the scene **once per board**. Each item is recorded
  with its kind, id, copper layers and net, and indexed by id and by net. Visibility, net
  isolation, highlight and active layer are flag flips over those records; nothing is
  rebuilt on mouse moves. Picking uses Qt's BSP index plus a precomputed item→record map.
  Pads and vias are painted with drill holes but hit-tested as solid.
- Scene units are millimetres; `_mm()` in the canvas is the only nm→display conversion.
- Panels (`project`, `layers`, `nets`, `inspector`, `log`) are dock widgets; their state
  and the window geometry persist in settings.
- Inspector property extraction is plain functions (`*_properties`) so it is testable
  without widgets; `None` renders as "unknown".

## 3. Domain layer (`pcbrouter.domain`)

- **Units:** one canonical representation — `int` nanometres (`Nm`). KiCad itself uses
  integer nanometres, and file values have at most 6 decimals of mm, so conversion is exact.
  Only `domain/units.py` converts (`mm_to_internal`, `internal_to_mm`, `parse_mm`);
  angles are `float` degrees in fields named `*_deg`.
- **Coordinates:** KiCad's — X right, Y down; positive rotation is counter-clockwise on
  screen. `rotate_point` is exact for multiples of 90°.
- **Immutability:** all domain objects are frozen dataclasses of tuples. Future routing
  produces *proposals* (new tracks/vias) against an immutable base board, which makes
  preview, diff, reject and undo cheap and safe.
- `Component` (reference, value, properties) is split from `Footprint` (geometry) so
  later stages can lock/constrain parts by designator without touching geometry.
- `BoardIndex` (lazy, cached per board) provides lookups by id/net and per-net statistics.
- Nets are keyed by **name**; numeric codes are informational (newer KiCad formats may
  reference nets by name only — the adapter already supports both).

## 4. KiCad adapter (`pcbrouter.kicad`)

- `parser.py`: a small, dependency-free S-expression parser. It reports line/column for
  malformed input and guards against pathological nesting. Chosen over third-party KiCad
  libraries because the mature ones are GPL-licensed or require KiCad's `pcbnew` module,
  and because the syntax is small enough to own completely.
- `adapter.py`: the only code that knows KiCad semantics (KiCad 5 `module`/`fp_text` and
  KiCad 6–9 `footprint`/`property`, 3-point and center/angle arcs, `*.Cu` wildcards,
  blind/micro vias, `(locked)` vs `(locked yes)`). Each item is parsed in isolation: a
  malformed footprint/track is skipped and reported as a `LoadWarning` with a line number;
  constructs Stage 1 does not display (zones, text, dimensions…) are counted and reported.
  Nothing is silently dropped or guessed.
- `loader.py`: validates the path/extension, reads bytes in `rb` mode, hashes them,
  decodes, parses, adapts and records timings. Errors: `KiCadLoadError` →
  `BoardFileNotFoundError`, `WrongFileTypeError`, `BoardPermissionError`,
  `MalformedBoardError`, `UnsupportedKiCadVersion`. Each carries a `user_message`.

## 5. Command bus (`pcbrouter.commands`)

All user-level operations are `BaseCommand` objects dispatched through `CommandBus`, whether
they come from the GUI, the CLI, or (later) the AI pipeline or a scripting API. The bus:

- refuses any command with `modifies_board = True` while `read_only` (all of Stage 1);
- converts exceptions into failed `CommandResult`s with a user message + technical detail;
- logs name, duration and outcome of every command; notifies subscribers.

Stage 1 commands: `OpenBoardCommand`, `CloseBoardCommand`, `BoardSummaryCommand`,
`ValidateAICommand` (parses and validates an AI-style command without executing it).

## 6. AI provider abstraction (`pcbrouter.ai`)

- `AIProvider` interface (`kind`, `config`, `is_configured()`, `complete()`); no
  implementations and no network code in Stage 1. Planned adapters: OpenAI, Anthropic,
  OpenAI-compatible endpoints, local models.
- `ProviderConfig` holds a `credential_ref` (name of an OS-keyring entry), never a key.
- `BoardContext` + `ContextPolicy` define what may be sent: summary by default, selection
  on request, full board only with explicit per-request consent.
- `command_schema.py`: a discriminated union of strict Pydantic models (`extra="forbid"`,
  bounded numbers, validated layer names, no free-form geometry): `analyze_board`,
  `route_net`, `route_group`, `route_board`, `set_constraint`, `lock_component`,
  `lock_track`, `protect_area`, `optimize_route`, `reduce_vias`.
  `parse_command()` does syntactic validation; `validate_against_board()` checks nets,
  layers, references and track ids against the loaded board. `command_json_schema()`
  exports the schema for provider structured-output features.

### Why the LLM never edits geometry

1. **Correctness:** LLMs are not geometric solvers; they cannot guarantee clearances,
   connectivity or manufacturability. A deterministic router + DRC can.
2. **Safety:** a model output (or a prompt injection hidden in a part description) must not
   be able to delete copper, move parts, or touch files. A closed command vocabulary with
   bounded parameters limits the blast radius to "a request the user reviews".
3. **Reproducibility:** the same validated command on the same board yields the same result,
   which makes testing and debugging possible.
4. **Review:** every change becomes a `RouteProposal` shown as a diff and applied only on
   explicit user acceptance.

## 7. Routing-engine boundary (future)

The router will live in its own package (e.g. `pcbrouter.routing`) and will expose
deterministic operations invoked only via commands. Inputs: an immutable `Board`, resolved
constraints, a `ComputeBackend`. Output: a `RouteProposal` (added/removed tracks and vias) —
never an in-place mutation. DRC runs on the proposal before it is shown.

## 8. Compute backends (`pcbrouter.compute`)

- `ComputeBackend` ABC: `name`, `kind`, `available`, `capabilities`, `device_info()`,
  `initialize()`, `shutdown()`.
- `CPUBackend` is always available. `GPUBackend` is a placeholder: never available, its
  `initialize()` raises `BackendUnavailableError`, and it reports detection results.
- `detection.py` never raises and never imports heavy libraries (only `find_spec`). It uses
  `nvidia-smi` (3 s timeout), then Linux sysfs PCI vendor ids or Windows CIM to classify:
  CUDA device detected / libraries not installed / unsupported GPU / CUDA unavailable.
  The GUI runs it in a background thread.
- `ComputeManager` honours the preferred backend only when available, otherwise falls back
  to the CPU and records why.

## 9. History system (`pcbrouter.history`)

- `HistoryManager`: undo/redo stacks of `UndoableAction`s with a depth limit and listeners.
  Stage 1 exercises it with metadata-only actions (no board edits exist yet).
- Future strategy: actions wrap small *change sets* relative to an immutable board rather
  than full board copies; snapshots are written only as restore points before saves.
- `SnapshotStore` / `MetadataSnapshotStore` (metadata only, writes nothing) and
  `RouteProposal` (pending → accepted/rejected, single transition) are in place.

## 10. Settings (`pcbrouter.settings`)

JSON via Pydantic (`extra="ignore"`, validated on assignment). Missing file → defaults;
invalid file → moved aside as `settings.json.corrupt-<timestamp>` and defaults used;
writes are atomic (temp file + `os.replace`). Future sections (`ai`, `routing`, `gpu`) exist
but are pinned to `enabled = false`. The schema contains no secret fields (tested).

## 11. Logging (`pcbrouter.app_logging`)

Named `app_logging` (not `logging`) to avoid any confusion with the standard library module.
Console + rotating file (2 MB × 5). Messages are `event.name key=value` for grep-ability.
A `SecretRedactingFilter` is attached to every handler (including the GUI log panel) and
masks API-key-shaped strings and `key=value` credentials.

## 12. Project safety model

- Opening a board reads it once in binary read-only mode; its SHA-256 is stored.
- Closing re-hashes the file and reports *unchanged* / *changed by another program* /
  *unreadable*. Integration tests assert SHA-256, size and mtime are identical and no side
  files appear after open → inspect → close, from the command bus, the GUI and the CLI.
- Each board has a **workspace** under the application data directory (keyed by a hash of
  its path, case-normalised on Windows) for future snapshots and proposals. Stage 1
  computes these paths but creates nothing.
- **Stage 9 will implement safe KiCad writing:** write to a temp file inside the workspace,
  re-parse it to validate, snapshot the previous version, then atomically replace — only on
  explicit user action, and never by an AI component.

## 13. Security rules

1. Never store plaintext API keys in project files or settings (OS keyring only).
2. Never embed API keys into PCB files.
3. Never send PCB files to an AI provider automatically.
4. Network operations require deliberate provider configuration.
5. Prompts send summarised/selected engineering context unless the user explicitly
   authorises more.
6. Never execute arbitrary code returned by an AI model.
7. Never log secrets.

## 14. Deviations from the Stage 1 brief's suggested layout

| Brief | Implemented | Reason |
|---|---|---|
| `ai-pcb-router/` top folder | repository root | The repository *is* the project root. |
| `pcbrouter/logging/` | `pcbrouter/app_logging/` | Avoid shadowing/confusion with stdlib `logging`. |
| — | `domain/units.py` | Single home for the canonical unit and its conversions. |
| — | `compute/manager.py` | Backend selection/fallback policy separate from backends. |
| — | `commands/board_commands.py` | Concrete read-only commands separate from the base classes. |
| — | `app/cli.py` | Headless mode proving UI and CLI share the command bus. |
| — | `ui/theme.py`, `ui/dialogs.py` | Shared colours/stylesheet and small dialogs. |
| — | `utils/paths.py` | Platform-aware directories with env overrides. |
| — | `tools/generate_synthetic_board.py` | Large synthetic boards for performance checks. |
