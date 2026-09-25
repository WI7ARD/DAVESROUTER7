# Stage 9 — reliability: safe KiCad export, sessions, recovery, audits

Version 0.9.0-stage9.

## Export (File ▸ Export Routed Board…, Ctrl+E)
`kicad/writer.py` is **append-only**. The output is the source file's own bytes,
with the new `(segment …)` and `(via …)` nodes inserted before the final `)`.
Nothing else in the file is re-serialised: components, zones, graphics, comments
and formatting stay exactly as KiCad wrote them.

1. The source SHA-256 must still match the file opened. Otherwise the export is
   refused with `EXPORT_BLOCKED_SOURCE_CHANGED`.
2. The file version must be 20211014–20251231 (KiCad 6–9). Otherwise
   `EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT`, and nothing is written.
3. The working board must contain all source copper, because export only adds.
   Otherwise `EXPORT_BLOCKED_UNSUPPORTED_CONSTRUCT`.
4. New nodes use the file's own conventions: `uuid` vs `tstamp`, quoted vs unquoted
   ids, net by code vs by name, CRLF vs LF.
5. **Self-check.** The composed text is parsed again with the loader. Its tracks
   and vias (ids, geometry, widths, layers, nets) and its component/zone counts must
   equal the working board. Otherwise `ERROR` is reported and nothing is written.
6. **Atomic write.** The file goes to a temp file in the same folder, is fsynced and
   then renamed; the written bytes are re-hashed.
7. **Provenance sidecar** `<out>.pcbrouter.json` records the object uuid, net and
   provenance (router/user/optimizer), the source and output SHA-256, and the
   verification result. KiCad has no generic property field on tracks, so tags are
   kept beside the board, never injected as invalid syntax.

Default output: `<name>_routed.kicad_pcb` (then `_routed-2`, …). Choosing the
source file in the export dialog is refused.

**DRC gate.** The Internal Geometry Check runs first:
- If it finds no errors, the status reads "Internal checks passed (N warnings)".
- If it finds errors, the export is `EXPORT_BLOCKED_DRC`. The user may explicitly
  export an **UNVERIFIED** copy (second confirmation), and the status line and the
  sidecar say so.

**KiCad DRC** (optional, Settings ▸ Export). When `kicad-cli` is installed, it
runs `kicad-cli pcb drc --format json` on the exported file, using argument lists
and a timeout. The result is labelled "KiCad DRC".

The app never says "manufacturing ready".

**Overwrite source** (File ▸ Overwrite Source Board…). This is available only when
Settings ▸ Export ▸ *Allow overwrite* is on; the command bus is read-only
otherwise. It needs a confirmation, and a backup copy is written to the workspace
first.

## Sessions and crash recovery
- **Session file** (`project/session_store.py`, format
  `pcbrouter-working-session` v1). It holds:
  - the source name, SHA-256 and fingerprint;
  - added tracks and vias with ids and provenance;
  - locks, locked regions, per-net constraints and corridors;
  - history labels.

  It never holds the board text or credentials; a save that mentions a key or
  Authorization header is refused.
- **Save/Load Session** (File menu). Loading is a validated commit ("Recovered
  session"), so illegal copper is rejected and the load can be undone. A session
  for a different SHA-256 is never applied.
- **Autosave.** The session is written to `<workspace>/recovery/working_session.json`
  1.5 s after each working-board change (atomic write).
- **On open:** if a recovery file exists for the same SHA-256, recovery is
  offered. Declining deletes the file.
- **On close:**
  - If the routed work is safe on disk (exported or saved), or nothing was routed,
    the recovery file is deleted.
  - If there is unexported routing, the file is kept, so it is offered next time
    instead of being lost.

## Diagnostic bundle (Help ▸ Export Diagnostic Bundle…)
A zip with two files:
- `diagnostics.json`: versions, OS/CPU, compute and GPU probe, kicad-cli version,
  settings (without recent-file paths or window state), and reproducibility data
  (source SHA-256, board fingerprint, history labels);
- `log_tail.txt`: the last 2000 log lines.

Both pass through the secret redactor. It contains no PCB file and no keys.

## Tests added
| file | covers |
|---|---|
| `tests/unit/test_export.py` | unmodified export byte-identical; routed export reload (same copper, connectivity, DRC); re-export fixed point; changed source / overwrite / wrong suffix / missing copper blocked; overwrite backup; KiCad 5 and v4 blocked; CRLF preserved; KiCad 6 token detection; DRC gate + unverified export; read-only overwrite; KiCad DRC report parsing; sessions (restore, undo, other SHA, corrupt, future version, credentials); illegal session copper rejected |
| `tests/integration/test_export_ui.py` | Ctrl+E export, source refusal, overwrite policy, DRC-gate prompt, overwrite with backup, session save/load, crash recovery offered/declined/kept/deleted, changed source not offered, diagnostic bundle contents |
| `tests/unit/test_reliability.py` | seeded fuzzing: 60 mutated KiCad files, 300 random AI payloads, 150 random segments (no crashes; legal commits keep DRC clean); properties: accepted routes valid, undo restores the fingerprint, connectivity monotone; reproducibility; golden corpus (`tests/fixtures/golden/corpus.json`: facts and DRC counts for 13 fixture boards; regenerate with `PCBROUTER_UPDATE_GOLDEN=1`); stress (120 random commit/undo steps); security audit |

## Security audit (automated in `test_reliability.py`)
- **Source tree:** `src/` contains no `eval(`, `exec(`, `shell=True`,
  `os.system(`, pickle, `yaml.load(` or hard-coded `sk-…` keys.
- **Subprocess calls** (nvidia-smi, PowerShell/wmic detection, kicad-cli) all use
  argument lists with timeouts.
- **AI modules** (`src/pcbrouter/ai/`) never write files, spawn processes or call
  the KiCad writer. Model output can only become schema-validated commands, which
  run through the command bus and the exact validator.
- **Paths:** export and session paths come from the user's file dialog only. The
  export refuses the source path and any non-`.kicad_pcb` name.

## Known limitations
- Export only **adds** copper. Rip-up of source copper is never exported; the
  router never rips up source copper anyway.
- **KiCad DRC: NOT RUN here.** kicad-cli is not installed in the development
  container. The report parser is tested with synthetic reports only.
- The golden corpus holds the repository's synthetic fixtures, not real customer
  boards.
