# Third-party notices

AI PCB Router bundles or works with the following third-party components.
Each remains under its own license; this file does not change those terms.

## Bundled in the installer

* **Qt / PySide6** (The Qt Company) — GNU Lesser General Public License v3
  (LGPLv3). You may replace the LGPL-covered libraries and reverse-engineer
  the Software to the extent required by the LGPL to debug such replacements.
* **NumPy, pydantic, openai, anthropic, keyring and their dependencies** —
  see each package's license (BSD/MIT/Apache-2.0 family, respectively).
* **Intel oneAPI runtime libraries** (SYCL, oneMKL, TBB, OpenMP) — Intel
  Simplified Software License; redistributed as installed by their official
  Python packages.
* **PyInstaller bootloader** — GPLv2 with a runtime exception for bundled
  applications.

## Separate programs (never bundled, never modified)

* **Freerouting** (GPL-3.0) — run as an external engine if you install it.
* **Ollama** and local models — their own licenses; board data sent to a
  *local* Ollama stays on your computer.
* **KiCad** (GPL-3.0) — optional; used only if installed (DSN/SES, DRC).

Full license texts ship with their respective packages. If any provision of
the AI PCB Router EULA conflicts with one of these licenses for that
component, the component's license governs that component.
