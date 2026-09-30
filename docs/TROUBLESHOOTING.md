# Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Chrome says the installer is *dangerous* / SmartScreen blocks it | The installer is not code-signed. Compare its SHA-256 with the release's `.sha256` file (`Get-FileHash file.exe -Algorithm SHA256`), then in Chrome: Downloads (Ctrl+J) → ⋮ → *Keep dangerous file*; SmartScreen: *More info* → *Run anyway*. |
| Every net fails with **RULE UNKNOWN** | The board's `.kicad_pro` is missing or has no net class values. Copy the `.kicad_pro` (and `.kicad_dru`) next to the `.kicad_pcb`, or set widths/clearances in KiCad's Board Setup ▸ Net Classes and save. |
| Some nets: *no legal path on the routing grid* | The net cannot be routed with the current rules and board space at the grid resolution. Try Accuracy mode, a finer grid (Settings ▸ Geometry), more passes, or route that area in KiCad by hand. The message gives the coordinates of the failed connection. |
| *not attempted: the board time budget ran out* | Large boards need more time: raise the budget (command line `--budget`), use Speed mode for the first pass, or enable parallel routing (Settings ▸ Routing). Routed nets are kept; route again to continue. |
| Export says **UNVERIFIED** | The internal check found errors on the board — often from copper or zone fills that were already in the source file. Open the Internal Geometry Check panel to see each one. |
| Routing card stays on "Preparing the board…" | Large boards (thousands of objects, big pours) take a few seconds to prepare; Cancel works at every step. |
| Cancel takes a moment | Cancel stops at the next check point (normally < 1 s); a stuck worker is stopped after 5 s. |
| GPU mode is slower or falls back to CPU | GPU routing is optional; the routing card says which backend actually ran and why. `pcbrouter.exe --gpu-check` prints a GPU report. |
| The app crashed | `%LOCALAPPDATA%\AI PCB Router\logs\crash.log` and `pcbrouter.log` contain the details; Help ▸ Export Diagnostic Bundle collects them. |
| Settings look wrong | Settings ▸ Restore Defaults. An unreadable settings file is reset automatically (a backup is kept). |
