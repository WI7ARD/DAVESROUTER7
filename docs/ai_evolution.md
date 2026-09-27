# Planner evolution (prompt strategies, ratings, benchmark)

Stage 10 / Phase 3 of the evolv merge. Planner instructions are versioned and
evidence-driven; nothing self-modifies, and the built-in default is always one
click away.

## Concepts

* **Strategy**: a named set of per-mode instruction overrides
  (`PromptStrategy`: id, name, note, parent, created time, overrides). Resolving
  merges overrides over the built-in `MODE_INSTRUCTIONS`. Versions are
  immutable: tuning creates a child; activating an older version is the rollback.
* **Ratings**: every answer in AI History can be marked helpful / not helpful
  (stored on the interaction, exported with history). Future benchmark runs can
  weight by them.
* **Benchmark** (`ai/evolution.py`, `tools/eval_planner.py`): deterministic, no
  model, no network — runs in CI:
  * prompt matrix: every mode × every strategy builds a valid request on real
    fixture boards (schema present, bounds respected, strategy id recorded);
  * golden replay: canned planner outputs (good analysis, good command, broken
    JSON, wrong shape) through parse → semantic validate, scored vs expected;
  * comparison: base vs candidate summary plus per-mode instruction diffs.
* **Live round** (`tools/eval_planner.py --live`): one real Ollama request per
  case per strategy; needs a local model and is slow on CPU by design.

## Strategy Lab (Settings ▸ AI Providers ▸ Planner strategy)

* **Snapshot Current…**: freeze the effective instructions as a named version.
* **Tune Mode…**: edit one mode's text; saved as a child version with a note.
* **Delete**: remove a version (activates built-in if it was active).
* Activating applies to new requests; the id travels in request metadata.

## Files

* `src/pcbrouter/ai/strategy.py` — records, resolve, snapshot, diff.
* `src/pcbrouter/ai/evolution.py` — deterministic benchmark + comparison.
* `tools/eval_planner.py` — CLI (`--boards`, `--strategy FILE`, `--live`).
* Ratings: `Interaction.rating`, history Helpful/Not Helpful buttons.
