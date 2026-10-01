# OpenBoards — pinned open-source KiCad projects

OpenBoards is DAVESROUTER's second real-board benchmark corpus. Real100
(`benchmarks/real100/`) pins 100 boards from a single repository (KiCad's QA data
and demos); OpenBoards pins **real open-source hardware projects from many
repositories**: keyboards, power electronics, dense SBC/carrier boards, FPGA and
MCU boards, RF boards, badges and small breakouts, with 2 to 8+ copper layers.

- Every board is pinned by **repository, commit and Git blob SHA-1**; so is each
  sidecar `.kicad_pro` / `.kicad_dru` (`fetch` refuses a file whose hash differs).
- Only KiCad 6+ projects with a `.kicad_pro` and a recognised open licence are
  accepted (see `ATTRIBUTION.md`).
- Board bytes are **not vendored**. `fetch` downloads them from
  `raw.githubusercontent.com` at the pinned commit; `prepare` makes local unrouted
  copies (top-level segments, vias and track arcs removed; footprints, zones,
  keepouts, rules and everything else kept) exactly like Real100.
- `reference_complete` records whether the original, routed board was fully
  connected. Most boards are; incomplete ones are kept only when they add
  something rare and are tagged `reference_incomplete`.

`CORPUS.md` lists every board with its layer count, routable (signal) layers, nets
to route, pad density, long-net share, routing demand, licence and
`reference_complete`, plus the rejected candidates and why.

## Running it

The Real100 harness runs any manifest given with the top-level `--manifest`
option (before the subcommand). Use a separate work folder:

```sh
M=benchmarks/openboards/manifest.json
W=benchmarks/openboards/work
python tools/benchmark_real100.py --manifest $M --workdir $W list
python tools/benchmark_real100.py --manifest $M --workdir $W fetch
python tools/benchmark_real100.py --manifest $M --workdir $W prepare
python tools/benchmark_real100.py --manifest $M --workdir $W inventory
python tools/benchmark_real100.py --manifest $M --workdir $W run --profile smoke
python tools/benchmark_real100.py --manifest $M --workdir $W report $W/runs/openboards-smoke-....jsonl
```

Profiles work as for Real100: `smoke` routes the first 12 boards in Speed mode
(45 s per board), `standard` every board except `route_policy: stress`, `full`
everything. Prepared boards land in `work/prepared/<id>/`, results in
`work/runs/openboards-<profile>-<stamp>.jsonl`.

Paired A/B of two strategies on this corpus:

```sh
python tools/real100_compare.py --baseline fixed --candidate policy.json \
    --manifest benchmarks/openboards/manifest.json --repeat 3 --profiles
```

`--manifest` is passed to every benchmark run; the work folder defaults to
`work/` next to the manifest.

Experience records written by benchmark runs identify boards by a salted hash.
`pcbrouter.benchmark.real100.board_hash(board_path, salt)` gives the same id for a
prepared board, so records can be matched to corpus boards.

## Curating

`curate.py` regenerates `manifest.json`, `CORPUS.md` and `ATTRIBUTION.md` from
`candidates.json` (a list of `{repo, ref, board, domain, tags, license_override?}`):

```sh
python benchmarks/openboards/curate.py               # evaluate all, write outputs
python benchmarks/openboards/curate.py --only machdyne/   # try a subset, no write
```

It resolves each ref to a commit (`git ls-remote`), downloads the board and
sidecars at that commit, maps the nearest `LICENSE`/`LICENCE`/`COPYING` to an SPDX
id, loads the board with the project loader, checks the original connectivity,
strips the routing and requires nets to route, and records the stripped board's
`learning.features.board_profile`. Downloads go to `work/curate/` (gitignored);
only the text files in this folder belong in git.
