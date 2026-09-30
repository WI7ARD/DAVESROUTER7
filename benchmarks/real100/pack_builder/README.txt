DAVESROUTER REAL100 — 100 REAL KICAD BOARDS + RULES
====================================================

Windows:
  Double-click DOWNLOAD_AND_BUILD_REAL100.bat

Linux/macOS:
  ./DOWNLOAD_AND_BUILD_REAL100.sh

What it does:
  - Downloads exactly 100 pinned KiCad PCB files from the official KiCad source mirror.
  - Verifies every ORIGINAL board against the Git blob SHA stored in manifest.json.
  - Removes all routed copper tracks: segments, vias, and curved track arcs.
  - Preserves footprints, pads, nets, board outlines, copper zones, and embedded rules/settings.
  - Fetches matching .kicad_pro and .kicad_dru files when they exist upstream.
  - Generates a per-board rules_inventory.csv and source metadata.
  - Produces DAVESROUTER-Real100-Unrouted-Boards-and-Rules.zip.

Pinned upstream commit:
  fec63a6f5197a584e27b1bc3e7b8431be9c05a78

The board corpus is roughly 198.5 MiB before sidecars/compression, so the completed ZIP can be large.

KiCad rules can exist in THREE places:
  1) .kicad_dru custom design rules
  2) .kicad_pro project/net-class settings
  3) rules/settings embedded directly inside .kicad_pcb

This builder keeps all three sources where available.

ROUTING STRIP POLICY
--------------------
The generated benchmark boards are intentionally UNROUTED derivatives.
Removed: top-level (segment ...), (via ...), and copper-track (arc ...) objects.
Preserved: zones, footprints, pads, nets, outline, setup, project files, and rule files.
Each K###/SOURCE.json records the exact removal counts and both source/unrouted SHA-256 hashes.
