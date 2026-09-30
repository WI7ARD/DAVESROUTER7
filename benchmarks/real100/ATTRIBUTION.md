# Real100 corpus attribution and licensing

DAVESROUTER Real100 references PCB files from the official KiCad source mirror:

`https://github.com/KiCad/kicad-source-mirror`

Pinned source commit:

`fec63a6f5197a584e27b1bc3e7b8431be9c05a78`

DAVESROUTER does not include those board files in this repository/archive. The benchmark fetcher downloads them from the pinned upstream source when the user requests the corpus.

KiCad's upstream `LICENSE.README` should be consulted for current licensing and third-party notices. It explicitly identifies `demos/*` as CC BY-SA 4.0. QA/source files are subject to the applicable KiCad repository and file-specific licensing/notices.

Upstream license references:

- `https://github.com/KiCad/kicad-source-mirror/blob/master/LICENSE.README`
- `https://github.com/KiCad/kicad-source-mirror/blob/master/LICENSE.CC-BY-SA-4.0`
- `https://github.com/KiCad/kicad-source-mirror/blob/master/LICENSE.GPLv3`

The locally generated `prepared/` boards are benchmark derivatives created on the user's machine. Do not redistribute them without following the applicable upstream license and attribution requirements.
