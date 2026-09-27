"""Deterministic planner benchmark for prompt-strategy evolution.

No model, no network: the same checks run in CI on every push.

* prompt matrix: every mode × every strategy builds a valid request on real
  fixture boards (schema present, bounds respected, strategy recorded);
* golden replay: canned planner outputs (good analysis, good command, broken
  JSON, schema-violating command) run through parse → validate, scored against
  expectations;
* comparison: base vs candidate strategy reports side by side with prompt diffs.

A live-model round (Ollama) stays manual: ``tools/eval_planner.py --live``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pcbrouter.ai.command_parser import parse_command_payload, parse_planner_response
from pcbrouter.ai.command_validator import SemanticValidator, SessionLocks, ValidationStatus
from pcbrouter.ai.conversation import Conversation
from pcbrouter.ai.exceptions import AIProviderError
from pcbrouter.ai.prompt_builder import PromptBuilder, PromptInputs
from pcbrouter.ai.requests import AIMode
from pcbrouter.ai.strategy import PromptStrategy, describe_diff, resolve_instructions
from pcbrouter.board_engine import BoardEngine
from pcbrouter.domain.board import Board
from pcbrouter.kicad.loader import load_board
from pcbrouter.kicad.rule_adapter import load_project_rules
from pcbrouter.routing.working_board import WorkingBoard

PROMPT = "Which nets are unrouted?"
TIMEOUT_S = 30.0
MAX_TOKENS = 512


@dataclass(frozen=True, slots=True)
class MatrixCase:
    name: str
    board_file: str
    mode: AIMode


MATRIX: tuple[MatrixCase, ...] = (
    MatrixCase("can-analyze", "can_node.kicad_pcb", AIMode.ANALYZE),
    MatrixCase("can-plan", "can_node.kicad_pcb", AIMode.PLAN),
    MatrixCase("can-command", "can_node.kicad_pcb", AIMode.COMMAND),
    MatrixCase("dense-explain", "router_dense.kicad_pcb", AIMode.EXPLAIN),
)


@dataclass
class PromptCheck:
    case: str
    strategy_id: str
    ok: bool
    detail: str = ""
    system_chars: int = 0

    @property
    def name(self) -> str:
        return self.case


@dataclass
class ReplayCheck:
    name: str
    expected: str  # "analysis" | "valid-command" | "invalid"
    got: str
    ok: bool
    detail: str = ""


@dataclass
class BenchmarkReport:
    strategy_id: str
    prompt_checks: list[PromptCheck] = field(default_factory=list)
    replay_checks: list[ReplayCheck] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def checks(self) -> list[PromptCheck | ReplayCheck]:
        return [*self.prompt_checks, *self.replay_checks]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def total(self) -> int:
        return len(self.checks)

    def summary(self) -> str:
        return f"strategy {self.strategy_id}: {self.passed}/{self.total} checks passed"


def _working(board_file: str, boards_dir: str) -> WorkingBoard:
    from pathlib import Path

    path = Path(boards_dir) / board_file
    return WorkingBoard(load_board(path).board, load_project_rules(path))


def benchmark_prompts(
    strategies: list[PromptStrategy | None],
    boards_dir: str,
    cases: tuple[MatrixCase, ...] = MATRIX,
) -> list[BenchmarkReport]:
    """Build every case under every strategy; score request validity."""
    from pcbrouter.ai.context_builder import BoardContextBuilder

    reports: list[BenchmarkReport] = []
    for strategy in strategies:
        report = BenchmarkReport(strategy.strategy_id if strategy else "builtin")
        t0 = time.perf_counter()
        for case in cases:
            try:
                wb = _working(case.board_file, boards_dir)
                engine = BoardEngine(wb.board)
                context = BoardContextBuilder(wb.board, engine=engine).build(user_prompt=PROMPT)
                request = PromptBuilder().build(
                    PromptInputs(
                        mode=case.mode,
                        user_prompt=PROMPT,
                        context=context,
                        model="benchmark",
                        timeout_s=TIMEOUT_S,
                        max_output_tokens=MAX_TOKENS,
                        strategy=strategy,
                    ),
                    Conversation(),
                )
            except (ValueError, KeyError) as exc:
                report.prompt_checks.append(
                    PromptCheck(case.name, report.strategy_id, False, str(exc))
                )
                continue
            problems: list[str] = []
            if request.response_schema is None:
                problems.append("missing response schema")
            if not request.system_prompt.strip():
                problems.append("empty system prompt")
            if request.timeout_s != TIMEOUT_S or request.max_output_tokens != MAX_TOKENS:
                problems.append("limits not honoured")
            meta_strategy = (request.metadata or {}).get("strategy_id")
            if meta_strategy != report.strategy_id:
                problems.append(f"strategy not recorded ({meta_strategy!r})")
            if case.mode is AIMode.COMMAND and "commands" not in request.system_prompt:
                problems.append("command mode instruction missing")
            report.prompt_checks.append(
                PromptCheck(
                    case.name,
                    report.strategy_id,
                    not problems,
                    "; ".join(problems),
                    len(request.system_prompt),
                )
            )
        report.elapsed_s = time.perf_counter() - t0
        reports.append(report)
    return reports


def _replay_board(boards_dir: str) -> Board:
    from pathlib import Path

    return load_board(Path(boards_dir) / "can_node.kicad_pcb").board


def benchmark_golden_replay(boards_dir: str, strategy_id: str = "builtin") -> BenchmarkReport:
    """Canned model outputs through parse → validate, scored vs expectations."""
    from pcbrouter.ai.anonymizer import AnonymizationOptions, Anonymizer

    report = BenchmarkReport(strategy_id)
    board = _replay_board(boards_dir)
    anon = Anonymizer(board, AnonymizationOptions())
    good_analysis = (
        '{"schema_version": 2, "mode": "analyze", '
        '"message": "Two nets need routing.", "analysis": null, "plan_steps": null, '
        '"commands": null, "clarification_needed": null, "unsupported_request": null}'
    )
    good_command = (
        '{"schema_version": 2, "mode": "command", "message": "Routing CAN.", '
        '"analysis": null, "plan_steps": null, '
        '"commands": [{"operation": "route_net", "targets": [{"type": "net", "name": "CAN_TXD"}], '
        '"constraints": {"preferred_trace_width_mm": 0.25}}], '
        '"clarification_needed": null, "unsupported_request": null}'
    )
    cases: list[tuple[str, str, str]] = [
        ("valid-analysis", good_analysis, "analysis"),
        ("valid-command", good_command, "valid-command"),
        ("broken-json", "{not json", "invalid"),
        ("wrong-shape", '{"mode": "analyze"}', "invalid"),
    ]
    for name, content, expected in cases:
        try:
            parsed = parse_planner_response(content)
        except AIProviderError as exc:
            got = "invalid"
            detail = exc.user_message[:120]
        else:
            response = parsed.response
            if expected == "analysis":
                got = "analysis" if response.message and not response.commands else "mismatch"
                detail = ""
            elif expected == "valid-command":
                verdicts = []
                for command in response.commands or []:
                    payload = command.model_dump(mode="json", exclude_none=True)
                    result = SemanticValidator(board, anon, SessionLocks()).validate(
                        parse_command_payload(payload)
                    )
                    verdicts.append(result.status)
                if verdicts and all(v is ValidationStatus.VALID for v in verdicts):
                    got = "valid-command"
                    detail = ""
                else:
                    got = "mismatch"
                    detail = f"command verdicts: {[v.value for v in verdicts]}"
            else:
                got = "mismatch"
                detail = "expected parse failure but parsed"
        report.replay_checks.append(ReplayCheck(name, expected, got, got == expected, detail))
    return report


def compare_reports(
    base: BenchmarkReport,
    candidate: BenchmarkReport,
    base_instructions: dict[AIMode, str] | None = None,
    candidate_instructions: dict[AIMode, str] | None = None,
) -> dict[str, object]:
    """Side-by-side summary for promoting (or rejecting) a candidate strategy."""
    out: dict[str, object] = {
        "base": base.summary(),
        "candidate": candidate.summary(),
        "base_failures": [c.name for c in base.checks if not c.ok],
        "candidate_failures": [c.name for c in candidate.checks if not c.ok],
    }
    if base_instructions is not None and candidate_instructions is not None:
        out["instruction_diff"] = describe_diff(base_instructions, candidate_instructions)
    return out


def benchmark_all(
    strategies: list[PromptStrategy | None], boards_dir: str
) -> tuple[list[BenchmarkReport], dict[str, object] | None]:
    """Full deterministic run; comparison is base vs first candidate when present."""
    prompt_reports = benchmark_prompts(strategies, boards_dir)
    golden = benchmark_golden_replay(boards_dir)
    if prompt_reports:
        prompt_reports[0].replay_checks.extend(golden.replay_checks)
    comparison = None
    if len(prompt_reports) > 1 and len(strategies) > 1:
        comparison = compare_reports(
            prompt_reports[0],
            prompt_reports[1],
            resolve_instructions(strategies[0]),
            resolve_instructions(strategies[1]),
        )
    return prompt_reports, comparison
