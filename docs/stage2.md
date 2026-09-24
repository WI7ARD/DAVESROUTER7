# Stage 2 — AI Provider Layer + Prompt-to-Constraint Compiler

Version `0.2.0-stage2`. The AI planner turns natural language into **validated,
previewable PCB commands**. Stage 2 stops at *command preview + approval*: it routes
nothing, moves nothing, modifies no copper and saves no files.

Details: [ai_architecture.md](ai_architecture.md) · [security.md](security.md).

## What was added

* **Providers**: OpenAI (Responses API), Anthropic (Messages API), any
  OpenAI-compatible endpoint (Chat Completions) — behind one `AIProvider` interface.
  SDKs are optional extras (`pip install "ai-pcb-router[ai]"`).
* **Credentials**: OS keyring (Save / Replace / Delete / Test), masked display,
  session-only memory fallback, no plaintext.
* **Settings ▸ AI Providers**: multiple profiles, default provider, model refresh or
  manual model ID, connection test, and assistant preferences (mode, context level,
  limits, timeout, retries, privacy disclosure, anonymisation, history saving, debug
  prompt logging).
* **AI Engineering panel** (Ctrl+I): provider / model / mode (Analyze, Plan, Command,
  Explain), example prompts, context size and preview, Send / Cancel with progress,
  conversation, proposal preview with claim labels, Approve / Reject / Edit
  Constraints, session usage. First-run empty state when no provider is configured.
* **AI History panel**, **AI ▸ Usage**, **AI ▸ Export AI Session**, **Edit ▸ Undo/Redo**
  of proposal decisions, **status-bar connection indicator** (last known state; no
  background pinging).
* **Compiler**: context builder (3 levels, limits, relevance, escaping, fingerprint),
  anonymiser, static system prompt, provider-independent prompt builder, v2 command
  schema (15 operations, typed targets, ≈27 typed constraints), strict-mode wire
  schema, safe parser, semantic validator, proposal state machine, history integration.

## Changes to Stage 1 code (all necessary for Stage 2)

| Change | Why |
|---|---|
| `ai/command_schema.py` replaced by the v2 envelope; Stage 1's compact form still accepted by the parser | the brief requires a substantially expanded schema; the CLI `--check-command` keeps working |
| `ValidateAICommand` uses the v2 parser + semantic validator | one set of rules for CLI and GUI |
| `ai/provider.py` rewritten; `ProviderKind` moved to `ai/profiles.py` (the unused `LOCAL` kind dropped: local servers use the compatible adapter) | real providers instead of an interface stub |
| `settings.AISettings` replaces the inert placeholder; settings schema v2 with migration | provider profiles and AI preferences |
| `CommandContext.ai`, `ProjectSession.session_id`, `Board.fingerprint` | commands reach the AI service; responses are tied to a board state |
| Main window: AI docks, AI/Edit menus, AI status label | UI |
| Tests updated deliberately: version string; AI settings no longer a placeholder; AI menu no longer "later stage"; Stage 1 v1-schema tests replaced by v2 schema tests | behaviour changed on purpose |

All other Stage 1 behaviour is unchanged and its tests pass unmodified.

## Tests

`pytest` — **392 passing** at the time of writing (Stage 1 regression + Stage 2), plus
`black --check`, `ruff check`, `mypy --strict` on `src/` and `tests/` — all clean.

| Suite | Covers |
|---|---|
| `unit/test_ai_command_schema.py` | valid route_net/route_group/analyze_board; negative width/clearance/max_vias; bad target types; duplicates; malformed JSON; `execute_shell` rejected; spec example; Stage 1 form |
| `unit/test_ai_validator.py` | spec item 50: CAN_H valid, CAN_X invalid with suggestions (no substitution), F.Cu vs Edge.Cuts, locked component, locked tracks, session locks, operation/target rules, consistency, warnings, confidence, anonymised mapping |
| `unit/test_ai_context_and_prompts.py` | facts, read-only tool surface, context levels & limits, fingerprint, **prompt injection (U99)**, delimiter escaping, anonymiser (reversible, collision-free, footprints), system-prompt rules, bounded conversation, strict wire schema |
| `unit/test_ai_credentials.py` | spec item 52: store/retrieve/replace/delete/missing; no plaintext fallback; plaintext backends refused; **settings file searched for the fake key** |
| `unit/test_provider_contracts.py` | spec item 48, ×3 adapters with the real SDKs over a mock transport: normalised responses, errors (401/403/404/429/500/503/timeout/connection/malformed), key redaction, repr, explicit endpoints, cancellation < 1 s, connection test sends no board data, missing key/SDK; adapter specifics (store=False, strict schema, fallback, stop reasons, real model capabilities, compatible format ladder, 1-token ping) |
| `unit/test_ai_session.py` | MockAIProvider scenarios (analysis, command, invalid JSON, invalid schema, unknown net, timeout, rate limit, auth, cancel), approval/undo, read-only execution, locks, edit traceability, staleness, refusal/truncation, anonymised round trip, export, retry policy, usage, **prompts not logged by default** |
| `integration/test_ai_local_server.py` | compatible adapter vs a real local HTTP server over real sockets |
| `integration/test_ai_ui.py` | first run/offline, provider configuration + key storage, connection indicator, cancelled-dialog key cleanup, send states, privacy disclosure once, full analyze→command→edit→approve→reject→undo flow with **SHA-256 and fingerprint unchanged**, invalid proposal not approvable, cancel (no duplicate message), board switch mid-request, friendly failures, restart retention, export, usage, editor covers every constraint, insecure http warning |
| `integration/test_optional_dependencies.py` | app, GUI and AI layer with openai/anthropic/keyring **blocked** |

### Real provider tests

**NOT RUN.** No OpenAI or Anthropic credentials were available in the build
environment, so no request reached a real model. What *was* verified: request bodies
and response parsing through the installed official SDKs (openai 3.19.2,
anthropic 1.8.0) against a mock transport, and the compatible adapter against a real
local HTTP server. To smoke-test with your keys: configure a profile, press
**Test Connection**, then ask "Analyze this board." on a fixture board.

## Known limitations

* Real-model behaviour (answer quality, whether each provider accepts the strict wire
  schema for every model) is unverified until run with real keys; schema rejection
  falls back to prompt-described JSON automatically.
* OS keyring integration was tested with an in-memory keyring backend; Windows
  Credential Manager and Linux Secret Service were not exercised (none in the build
  container, which correctly fell back to "session only").
* Net classes and `.kicad_pro` design rules are not loaded, so `avoid_net_classes`
  and similar constraints are recorded but unverifiable (reported as warnings).
* "Unrouted" means "≥ 2 pads and no copper"; connectivity is not analysed yet.
* Session locks/approved constraints live in the AI session (and exports); they are
  not yet persisted as a project constraint file.
* Token counts are estimates (≈4 characters/token) unless `tiktoken` is installed.
* OpenAI's model list includes non-chat models (the API gives no capability data).
* Windows: the full test suite and an install → run → uninstall smoke test of the
  installer pass on Windows Server 2025 (GitHub Actions); the installer and app were
  also confirmed working by hand on the developer's Windows PC. AI features were not
  exercised with real provider keys there.

## Stage 3 prerequisites

1. Load net classes and rules from `.kicad_pro` / `.kicad_dru` so constraints such as
   `avoid_net_classes` and board minimums become verifiable.
2. Connectivity graph / ratsnest for real routed/unrouted status.
3. Spatial index + deterministic DRC engine to produce genuine **DRC RESULT** claims.
4. A persistent, versioned constraint set in the workspace (approved AI constraints
   become inputs to routing).
5. Zones/keepouts in the domain model (needed by `protect_area` and routing).

## Stage 2 acceptance checklist

| # | Requirement | Result | Evidence / note |
|---|---|---|---|
| 1 | Start the Stage 1 application | PASS | real `main()` GUI test |
| 2 | Open a KiCad PCB | PASS | UI tests |
| 3 | Inspect the PCB exactly as before | PASS | all Stage 1 inspection tests unchanged and passing |
| 4 | Open AI Provider Settings | PASS | `test_first_run_and_offline_mode` |
| 5 | Configure OpenAI | PASS | `test_provider_configuration_and_key_storage` |
| 6 | Securely save an OpenAI key | PARTIAL | storage logic tested with an in-memory keyring backend; real OS keychains not exercised |
| 7 | Test the OpenAI connection | PARTIAL | adapter tested through the real SDK with a mock transport; no real OpenAI call (no key) |
| 8 | Configure Anthropic | PASS | settings-widget tests |
| 9 | Securely save an Anthropic key | PARTIAL | as #6 |
| 10 | Configure a custom OpenAI-compatible provider | PASS | UI test + real local HTTP server test |
| 11 | Choose a provider and model | PASS | UI tests |
| 12 | Open the AI Engineering panel | PASS | UI tests |
| 13 | Ask "Analyze this board." | PASS | mock provider through the real runner |
| 14 | See a real model response | NOT RUN | no credentials available |
| 15 | Ask "Route CAN first and minimize vias." | PASS | mock provider |
| 16 | Receive a structured routing proposal | PARTIAL | full pipeline verified with mock responses only |
| 17 | See whether referenced nets/layers exist | PASS | validation checklist in preview |
| 18 | See validation warnings/errors | PASS | preview + invalid-proposal test |
| 19 | Edit the proposed constraints | PASS | editor round-trip + traceability |
| 20 | Approve the proposal | PASS | |
| 21 | See it stored in command history | PASS | AI History panel + HistoryManager |
| 22 | Confirm NO PCB geometry changed | PASS | fingerprint, render counts and SHA-256 unchanged |
| 23 | Reject another proposal | PASS | |
| 24 | Cancel an active AI request | PASS | no duplicate message; UI usable again |
| 25 | Use the program offline without AI | PASS | first-run test; SDKs/keyring blocked test |
| 26 | Restart and retain provider configuration | PASS | `test_restart_retains_provider_configuration` |
| 27 | Verify API keys are not in settings files | PASS | file searched for the fake key |
| 28 | Run the complete test suite | PASS | 392 passed |
| — | Real-provider manual smoke test (item 71) | NOT RUN | no credentials |
| — | Windows 11 | PASS | test suite + installer smoke test pass on Windows Server 2025 (CI); installer and app confirmed by hand on the developer's Windows PC |
