# Routing with Freerouting

[Freerouting](https://github.com/freerouting/freerouting) is a mature open-source
autorouter used by many KiCad users. AI PCB Router runs it as a **separate program**
and checks everything it produces before you can accept it.

## License
Freerouting is **GPL-3.0**. This app (MIT) does **not include, copy or modify** it:
you install Freerouting yourself and the app starts it as an external process, the
same way you would run it by hand.

## What you need
1. **KiCad 7–10.** KiCad's own Python (`pcbnew`) converts the board to Freerouting's
   Specctra format (DSN) and imports the result (SES). It is found next to
   `kicad-cli` (`C:\Program Files\KiCad\<version>\bin\python.exe`); override with
   `PCBROUTER_KICAD_PYTHON`.
2. **Freerouting**, one of:
   - the Windows installer `freerouting-<ver>-windows-x64.msi` from
     <https://github.com/freerouting/freerouting/releases> (includes Java; easiest), or
   - `freerouting-<ver>.jar` plus Java 21+ (`winget install EclipseAdoptium.Temurin.21.JRE`).

   Found via Settings (`routing.freerouting_path`), `PCBROUTER_FREEROUTING`, the app
   data folder `freerouting\`, the standard install folders, or `PATH`.

**Tools ▸ Set Up Freerouting…** checks all of this and has a button for each step
(download page, download the .jar, choose a file, install Java, get KiCad, and
**Test on open board**, which runs one pass on a copy and changes nothing).

## Routing
**Router ▸ Route Board with Freerouting…** runs in the routing worker process, so
the window stays responsive. The overlay shows the pass number and unrouted count;
**Cancel** kills Freerouting.

1. The working board (source + your accepted routes) is written to a temp file.
2. KiCad exports DSN with **all existing copper locked**, so Freerouting only adds.
3. Freerouting runs headless: `-de in.dsn -do out.ses -mp <passes>`.
4. KiCad imports the SES into a temp copy; the app loads it and diffs the copper.
5. Each net's new copper is committed on a fork through the **exact validator**.
   Nets it rejects are listed ("rejected by the exact validator: …") and never added.
6. The result appears in **Routing Jobs**: accept all or per net (undoable).
7. **File ▸ Export Routed Board…** writes a new file (DRC gate, reload self-check).
   The source board is never modified.

## Command line
```
pcbrouter --setup-freerouting [BOARD.kicad_pcb]      # report; with BOARD, test one pass
pcbrouter --freeroute BOARD.kicad_pcb --output OUT.kicad_pcb [--passes N]
```
`--freeroute` refuses to overwrite the source and exports through the same
validated pipeline as the app.

## KiCad versions
Boards from KiCad 6–10 (file format up to 20261231) can be routed and exported.
KiCad 10 references nets by name (`(net "GND")`); new copper is written the same
way. Newer formats are refused until tested.

## Testing status
- Automated tests use stand-ins for KiCad's Python and for Freerouting
  (`tests/support/fake_pcbnew`, `tests/support/fake_freerouting.py`).
- Real KiCad + Freerouting runs have **not** been tested in CI yet; they need a
  machine with both installed.
- Known risk: `pcbnew`'s SWIG API is deprecated in newer KiCad. If
  `ExportSpecctraDSN`/`ImportSpecctraSES` are missing, the Test button reports it.
