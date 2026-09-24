# AI Architecture (Stage 2)

> **The AI model is an engineering planner, not the routing engine.**
> It may analyse, recommend, prioritise and express intent as structured commands.
> It can never edit geometry, files or code. **No geometry modification in Stage 2.**

## 1. Pipeline

```
                     ┌───────────────┐
                     │  User Prompt  │  (AI Engineering panel)
                     └───────┬───────┘
                             │
              ┌──────────────▼─────────────┐
              │ BoardContextBuilder        │  compact facts from the domain model,
              │  + Anonymizer (optional)   │  never the .kicad_pcb file
              └──────────────┬─────────────┘
                             │
                    ┌────────▼────────┐
                    │  PromptBuilder  │  static system prompt + <pcb_context> data
                    └────────┬────────┘  + <session_state> + <user_request>
                             │
                     ┌───────▼───────┐
                     │  AIProvider   │  OpenAI (Responses API)
                     ├───────────────┤  Anthropic (Messages API)
                     │  via runner   │  OpenAI-compatible (Chat Completions)
                     └───────┬───────┘  background asyncio loop, retries, cancel
                             │
                   ┌─────────▼─────────┐
                   │ Structured Output │  native JSON schema where supported,
                   └─────────┬─────────┘  schema-in-prompt fallback otherwise
                             │
                   ┌─────────▼──────────┐
                   │ command_parser     │  JSON only, nulls stripped, strict Pydantic
                   └─────────┬──────────┘
                             │
                  ┌──────────▼──────────┐
                  │ command_validator   │  nets/components/layers exist, locks,
                  └──────────┬──────────┘  consistency, suggestions (never substitution)
                             │
                    ┌────────▼────────┐
                    │ Command Preview │  BOARD FACT / AI OBSERVATION / DRC / USER
                    └────────┬────────┘
                             │  user edits, approves or rejects
                    ┌────────▼────────┐
                    │ Command History │  HistoryManager (undoable), export
                    └────────┬────────┘
                             │
                     (Stage 4+ routing engine — deterministic)
```

## 2. Modules (`src/pcbrouter/ai/`)

| Module | Responsibility |
|---|---|
| `provider.py` | `AIProvider` ABC: `provider_id`, `display_name`, `capabilities()`, `async test_connection()`, `async list_models()`, `async generate()`; safe `repr()` |
| `openai_provider.py` | OpenAI **Responses API** (`responses.create`, `text.format` strict JSON schema, `store=False`) |
| `anthropic_provider.py` | Anthropic **Messages API** (`messages.create`, `output_config.format` JSON schema when the Models API says the model supports it) |
| `openai_compatible_provider.py` | Chat Completions for LM Studio/Ollama/vLLM…: optional key, format ladder `json_schema → json_object → prompt` |
| `_sdk_common.py` | lazy SDK import, credential lookup, SDK-exception → app-exception mapping, schema fallback, client lifecycle |
| `provider_registry.py` | profile → adapter factory (injectable), package availability |
| `profiles.py` | `ProviderProfile` (non-secret config: kind, name, model, base URL, timeout, `credential_ref`) |
| `credentials.py` | OS keyring via `keyring`; session-only memory store; no plaintext fallback |
| `models.py`, `requests.py`, `responses.py` | provider-neutral `AIModelInfo`, `ProviderCapabilities`, `ConnectionResult`, `AIRequest`, `AIResponse`, `TokenUsage` |
| `exceptions.py` | `AIProviderError` hierarchy (auth, rate limit, timeout, connection, server, model not found, invalid request/response, schema, cancelled) |
| `retry.py` | bounded exponential backoff; retry only transient errors; rate limits only with a short `Retry-After` |
| `runner.py` | private asyncio loop thread; true cancellation; overall deadline |
| `board_summary.py` | deterministic `BoardFactService` (+ read-only tool registry for future agent use) |
| `anonymizer.py` | reversible, collision-free tokens for nets / references / values+footprints / filename |
| `context_builder.py` | `ContextLevel` (minimal/standard/detailed), limits, relevance ordering, escaping, fingerprint |
| `system_prompts.py`, `prompt_builder.py` | static planner instructions; provider-independent message layout |
| `command_schema.py` | v2 schema: `Operation` (15 ops), typed targets, `RoutingConstraints`, `AICommand`, `AIAnalysis`, `PlannerResponse` |
| `wire_schema.py` | provider-friendly strict-mode JSON Schema derived from the Pydantic models |
| `command_parser.py` | safe deserialisation; Stage 1 compact-form conversion |
| `command_validator.py` | semantic validation → `VALID` / `VALID_WITH_WARNINGS` / `INVALID` with checks and suggestions |
| `proposals.py` | `CommandProposal` state machine; original vs user-edited command |
| `conversation.py` | bounded per-board conversation |
| `session.py` | `AISession`: prepare → accept/record, approve/reject/edit via history, staleness, export |
| `service.py` | app-level `AIService`: credentials, registry, runner, usage, connection states, current session |
| `usage.py` | `UsageTracker`, approximate token estimation (no pricing) |

UI (`src/pcbrouter/ui/`): `ai_panel.py` (dock), `ai_controller.py` (Qt bridge),
`ai_provider_settings.py` (Settings ▸ AI Providers), `ai_dialogs.py` (privacy, context
preview, constraint editor, usage), `ai_history_panel.py`, `ai_render.py` (escaped HTML).
Bus commands: `commands/ai_commands.py` (`Approve/Reject/EditProposalCommand`).

## 3. Providers

**OpenAI.** Official SDK (tested with 3.19), `AsyncOpenAI(max_retries=0)` with explicit
`base_url` and key. Uses `client.responses.create(model, instructions, input,
text={"format": {"type": "json_schema", "strict": true, ...}}, max_output_tokens,
store=False)`. Parses `output_text`, refusals, `status` (`completed`/`incomplete`),
usage. Model discovery via `models.list()` (IDs only — capabilities stay unknown);
connection test via `models.retrieve(model)`. Organization/project are optional
profile fields.

**Anthropic.** Official SDK (tested with 1.8), `AsyncAnthropic(max_retries=0)`.
`messages.create(model, max_tokens, system, messages, output_config=...)`. The Models
API reports `max_input_tokens`, `max_tokens` and `capabilities.structured_outputs`;
these populate `AIModelInfo` and decide native vs prompt schema. Stop reasons map to
complete / truncated / refused.

**OpenAI-compatible.** Same SDK, Chat Completions. Key optional (placeholder bearer
sent to local servers). `/models` may be missing → listing reports *unsupported*, the
connection test falls back to a 1-token `ping`. Response-format support is probed
(`json_schema` → `json_object` → none) and remembered per endpoint+model.

Common rules: no sampling parameters are forced (current models reject some);
model IDs are configuration, never code; the SDKs' `*_BASE_URL`/`*_API_KEY`
environment variables are never used implicitly.

## 4. Structured output: two schemas

* The **wire schema** (`wire_schema.py`) guides generation. It is OpenAI-strict
  compatible: every object closed (`additionalProperties: false`), every property
  required, optional ones nullable, no `$ref`, no bounds or patterns (~7.5 KB).
* The **Pydantic models** are the authority. The parser strips `null`s (meaning "not
  specified") and validates every bound, pattern and cross-field rule locally —
  whatever the provider claims it enforced.

## 5. Command model

`AICommand` = `operation` + typed `targets` (`net`, `net_group`, `component`, `area`,
`board`) + `RoutingConstraints` (≈27 typed fields: layers, widths, clearance, vias, via
type, preserve/rip-up/movement, priority, criticality, lengths, avoid/keep-near,
differential pair, gap/skew, impedance, shielding, notes) + `reasoning_summary`
(a brief justification, not chain-of-thought) + `warnings` + `confidence` +
`requires_user_confirmation`. Operations fall in three categories:

| Category | Operations | On approval |
|---|---|---|
| read-only | analyze_board/net/component, explain_route | EXECUTED (nothing changes) |
| constraint | set_net_constraint, set_routing_priority, lock_component/net/track, protect_area | APPROVED; session constraint state updated (locks) |
| routing | route_net/group/board, optimize_net, reduce_vias | APPROVED — "Waiting for routing engine support (Stage 4)" |

Confidence never overrides validation.

## 6. Validation

`SemanticValidator` resolves anonymised tokens (exact lookup only), then checks: entity
existence (with fuzzy *suggestions*, never substitution), operation/target
compatibility and counts, duplicate targets, copper-only layers, preferred ∩ forbidden,
width ordering and board minimums, length/tolerance logic, rip-up vs preserve, via type
vs layer count, avoided/kept entities, locked components (board or session) and locked
tracks. Unverifiable intent (net classes, differential-pair rules, impedance) is a
*warning*, never silently accepted as enforced.

## 7. Context, privacy and injection resistance

* Context levels: MINIMAL (statistics + mentioned/selected), STANDARD (+ relevant nets
  and components), DETAILED (+ relationships, all net names).
* Limits: characters, nets, components; relevance ordering (mentioned/selected first,
  then unrouted, then most-connected); omissions are stated explicitly.
* All board-derived strings are JSON string literals with `<`/`>` escaped, inside
  `<pcb_context>`; the system prompt states that everything inside is untrusted data.
  A component value like "IGNORE ALL PREVIOUS INSTRUCTIONS…" stays a quoted value.
* Privacy disclosure (first request per board), context preview, optional
  anonymisation (footprint names are hidden together with component values because
  they often contain part numbers).

## 8. Workflow safety

* One request at a time; the background runner keeps the UI responsive; Cancel aborts
  the HTTP request, and late results are ignored.
* Every response is checked against the request id, board session id and board
  **fingerprint**; a mismatch yields a STALE interaction whose proposals are EXPIRED.
* Approvals/rejections are `DecisionAction`s in `HistoryManager` (undo/redo) with
  metadata: provider, model, prompt, context fingerprint, AI proposal, final command,
  user changes, validation result — never keys.

## 9. Future: deterministic board-query tools

`BoardFactService.TOOLS` (`get_board_statistics`, `get_net_info`, `get_component_info`,
`get_layer_info`) is a read-only, argument-checked tool surface. A later stage can let
an agent ask for facts instead of receiving a large context. There is no filesystem,
shell or network access in that surface.
