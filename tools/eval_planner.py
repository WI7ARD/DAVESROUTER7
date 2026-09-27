"""Deterministic planner benchmark (no model, no network).

Usage:
    python tools/eval_planner.py [--boards DIR] [--strategy FILE] [--live]

Without --live: prompt-build matrix + golden replay on fixtures for the
built-in instructions and, with --strategy, a saved PromptStrategy JSON file.
With --live: also send one real request per case through Ollama (needs a local
model; slow on CPU) and report finish/timeout per strategy.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _cmd() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--boards", default=str(ROOT / "tests" / "fixtures" / "boards"))
    parser.add_argument("--strategy", default=None, help="PromptStrategy JSON file")
    parser.add_argument("--live", action="store_true", help="also query local Ollama")
    parser.add_argument("--model", default="qwen2.5:7b")
    return parser.parse_args()


def main() -> int:
    from pcbrouter.ai.evolution import benchmark_all
    from pcbrouter.ai.strategy import PromptStrategy

    args = _cmd()
    strategies: list = [None]
    if args.strategy:
        strategies.append(
            PromptStrategy.model_validate_json(Path(args.strategy).read_text(encoding="utf-8"))
        )
    reports, comparison = benchmark_all(strategies, args.boards)
    failed = False
    for report in reports:
        print(f"== {report.summary()} ({report.elapsed_s:.1f}s)")
        for check in (*report.prompt_checks, *report.replay_checks):
            name = getattr(check, "name", getattr(check, "case", "?"))
            flag = "ok  " if check.ok else "FAIL"
            failed = failed or not check.ok
            extra = f" — {check.detail}" if check.detail else ""
            print(f"  {flag} {name}{extra}")
    if comparison is not None:
        print("== comparison")
        print(json.dumps(comparison, indent=2, default=str))
    if args.live:
        failed = _live(args, strategies) or failed
    return 1 if failed else 0


def _live(args: argparse.Namespace, strategies: list) -> bool:
    """One real Ollama request per case per strategy (slow on CPU by design)."""
    from pcbrouter.ai.ollama import ollama_profile, ollama_status
    from pcbrouter.ai.requests import AIMode
    from pcbrouter.ai.service import AIService
    from pcbrouter.ai.session import AIRuntimeConfig
    from pcbrouter.kicad.loader import load_board

    status = ollama_status()
    if not status.running or args.model not in status.models:
        print(f"live: {args.model} not in Ollama ({status.text()[:100]}); skipped")
        return False
    failed = False
    board = load_board(Path(args.boards) / "can_node.kicad_pcb").board
    for strategy in strategies:
        label = strategy.strategy_id if strategy else "builtin"

        config = AIRuntimeConfig(timeout_s=300.0, max_output_tokens=2048, strategy=strategy)
        svc = AIService()
        try:
            session = svc.start_session(board, f"eval-{label}", config)
            profile = ollama_profile(args.model)
            prepared = session.prepare(
                "Which nets are unrouted?",
                AIMode.ANALYZE,
                model=args.model,
                provider_name=profile.name,
                timeout_s=300.0,
            )
            t0 = time.monotonic()
            try:
                response = svc.submit(prepared, profile, max_retries=0).result(timeout=330)
            except Exception as exc:
                print(f"live [{label}]: FAILED {type(exc).__name__}: {exc}")
                failed = True
                continue
            inter = session.accept_response(prepared, response)
            print(
                f"live [{label}]: {inter.kind} in {time.monotonic() - t0:.0f}s "
                f"({len(response.content)} chars)"
            )
        finally:
            svc.shutdown()
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
