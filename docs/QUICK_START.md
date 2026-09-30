# Quick start

1. **Install.** Run `AI-PCB-Router-<version>-Setup-x64.exe` (per-user, no admin
   rights needed). The installer is not code-signed: if Chrome or SmartScreen warns,
   compare the file's SHA-256 with the `.sha256` file from the release page
   (`Get-FileHash <file> -Algorithm SHA256` in PowerShell), then keep/run it.
2. **Start** *AI PCB Router* from the Start menu. A welcome page explains the workflow
   (Help ▸ Welcome shows it again).
3. **Open a board**: File ▸ Open Board… (Ctrl+O) → your `.kicad_pcb`. Keep the
   board's `.kicad_pro` (and `.kicad_dru`, if you have one) in the same folder — the
   design rules come from there.
4. **Pick a mode** in the Route panel: **Speed** (fast first pass) or **Accuracy**
   (default: shorter routes, fewer vias).
5. **Route**: Router ▸ Route Board (Ctrl+Shift+R). The routing card shows the phase,
   "net n of N", elapsed time and **Cancel**; the window stays usable.
6. **Review**: the result appears in *Routing Jobs* with a summary (nets routed,
   vias, length, time). Click **Accept All** (or accept individual nets). Nets that
   could not be routed are listed with the reason.
7. **Check**: Tools ▸ Internal Geometry Check (clearances, widths, vias, edge,
   connectivity).
8. **Save**: File ▸ Export Routed Board… (Ctrl+E) writes a **new** file,
   `<name>_routed.kicad_pcb`, next to the original. The original is not changed.
9. Open the exported board in KiCad, refill zones (B) and run KiCad's DRC before
   manufacturing.

Command line (no window): `pcbrouter.exe board.kicad_pcb --route --mode speed
--output routed.kicad_pcb --report result.json`. Exit code 0 means fully routed,
3 partially routed (file written), 1 an error (nothing written). `pcbrouter.exe --help`
lists every option.
