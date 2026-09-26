# Security Model

## Rules (in force since Stage 1, implemented in Stage 2)

1. **No plaintext API keys** in settings, projects, PCB files, logs, crash reports,
   history or AI-session exports.
2. **Keys live in the OS credential store** (`keyring`: Windows Credential Manager,
   macOS Keychain, Linux Secret Service/KWallet). Settings store only
   `credential_ref` (e.g. `ai-pcb-router/openai-main`). If no secure store exists, a
   key can be held **in memory for this session only**; there is no plaintext
   fallback, and file-based `keyrings.alt` backends are refused.
3. **No automatic uploads.** Nothing is sent to a provider until the user configures
   it and presses Send. Connection tests send no board data.
4. **The KiCad file is never uploaded.** Providers receive a compact, bounded summary;
   the user can preview it, and a disclosure is shown before the first request per
   board.
5. **The LLM never edits geometry, files or code.** Output is parsed as JSON only
   (never `eval`/`exec`/pickle/YAML), validated against a closed schema and the board,
   previewed, and needs explicit approval. Stage 2 executes no routing at all.
6. **Board text is data, not instructions.** It is quoted/escaped inside
   `<pcb_context>` and the system prompt says so.
7. **No secrets in logs.** A redaction filter masks key-shaped strings on every log
   handler; provider error messages are redacted before display; full prompts/context
   are logged only if the user enables the debug option.

## Where secrets can and cannot appear

| Place | Key present? | How it is enforced |
|---|---|---|
| OS keyring / in-memory session store | yes (by design) | `CredentialService` |
| SDK client object during one request | yes (briefly) | created per request, closed after |
| HTTP `Authorization` / `x-api-key` header to the provider | yes (by design) | SDK |
| `settings.json` | **no** | only `credential_ref`; test searches the file for a fake key |
| Logs, log panel | **no** | redaction filter + tests |
| Error dialogs / conversation | **no** | `AIProviderError` redacts messages; tests with key-bearing 401 bodies |
| History entries, AI-session export | **no** | metadata whitelist; tests |
| `repr()` of providers/profiles/secrets | **no** | `SecretStr`, custom `__repr__`; tests |

## Threats considered

| Threat | Mitigation |
|---|---|
| Prompt injection via component values/net names | escaped quoted data in delimiters; system prompt; schema cannot express actions beyond 15 bounded operations; semantic validation; human approval |
| Model invents nets/components | validator rejects unknown entities with suggestions; no auto-substitution |
| Model emits code / shell / file text | no field can carry it; unknown keys rejected; nothing is executed |
| Malicious/unexpected server response (proxy HTML, truncated JSON) | adapter shape checks; JSON-only parser; size limits |
| Response arriving for a different board | request id + session id + fingerprint → STALE, never approvable |
| Endpoint redirection via environment variables | base URL and key passed explicitly to SDKs |
| Key sent in clear to a remote `http://` endpoint | profile flags non-local `http` base URLs (`is_insecure_remote_http`) |
| Orphaned keys | keys saved in a cancelled dialog or for removed profiles are deleted |
| HTML/markup injection into the UI from model output | all model/board text HTML-escaped; links disabled |

## Audit (Stage 2)

Searched for API keys (`sk-`, `x-api-key`, `Authorization:`), `eval`, `exec`,
`pickle`, `yaml.load`, `shell=True`, `os.system`, `subprocess`, debug prints,
TODO/FIXME, `NotImplementedError`, active mocks and board-writing code. Findings: only
obviously fake test keys; `subprocess` only in Stage 1 GPU detection (fixed argument
lists, no shell, timeout); `.exec()` hits are Qt dialog calls; the only file writes are
settings (atomic) and user-requested AI-session JSON exports, which refuse
`.kicad_pcb` targets. No mock is reachable from application code (the mock provider
lives under `tests/`).
