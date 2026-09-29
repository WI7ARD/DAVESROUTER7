# Stability contracts (1.x)

What the 1.x line will not break out from under users, scripts, and files.

## Settings

* `settings.json` carries `schema_version` (currently 2). Unknown future
  versions load through `_migrate()`; a corrupt file is backed up and reset,
  never deleted silently. New preference fields always have defaults, so old
  files keep working.

## Command line

* `pcbrouter [BOARD]`, `--inspect`, `--check-command JSON`, `--version`,
  `--diagnostics`, `--gpu-check`, `--worker-selftest`, `--forget-api-keys`,
  `--setup-gpu`, `--setup-ollama [MODEL]`, `--setup-freerouting`,
  `--freeroute --output OUT` keep their names and JSON shapes. New flags may
  be added; existing ones keep working.

## Files the app owns (never your KiCad project)

* Board workspaces (`workspaces/<board-hash>/`): snapshots, route proposals,
  `ai_memory.json` (`{format_version, fingerprint, entries[]}`),
  `ai_sessions/`. Unknown files inside a workspace are ignored, never pruned.
* Source `.kicad_pcb` files are opened read-only (SHA-256 verified); exports
  always write a **new** file.

## Network and privacy

* No telemetry, no update checks, no background network. Network happens only
  on explicit user action:
  * AI providers you configure (OpenAI/Anthropic/compatible) — bounded,
    previewable summaries only, never the `.kicad_pcb` file;
  * Keyed profiles refuse plain-HTTP to non-local hosts (keys would travel in
    clear); local loopback HTTP (Ollama-style) and keyless endpoints are allowed;
  * Freerouting release lookup during Freerouting setup.
* API keys live in the OS credential store (or session memory); never in
  settings, logs, history, or exports.

## Third-party licenses in the bundle

* `THIRD-PARTY-NOTICES.md` and `LICENSE` ship at the top of the installed app
  (bundled by `pcbrouter.spec`, removed again by the exact-file uninstaller).
  Qt/PySide6 LGPL rights (including library replacement) are preserved.

## Known limitations (1.x)

* Placed vias always span the full copper stack: a via the search uses between
  two inner layers is still committed (and validated) as a through via.
  Blind/buried intent is not preserved.
* Large dense boards can need minutes per net in Accuracy mode. The Speed toggle
  (coarser grid, bounded weighted search) is the intended first pass (typically
  the majority of nets); Accuracy finishes the leftovers.
