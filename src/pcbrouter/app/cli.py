"""Headless command-line mode. Uses exactly the same commands as the GUI."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from pcbrouter.commands import BoardSummaryCommand, OpenBoardCommand, ValidateAICommand
from pcbrouter.settings.settings import AppSettings

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LOAD_FAILED = 4
EXIT_COMMAND_INVALID = 5


def run_cli(args: argparse.Namespace) -> int:
    from pcbrouter.app.application import build_services

    if args.board is None:
        print("error: --inspect/--check-command need a BOARD path", file=sys.stderr)
        return EXIT_USAGE
    services = build_services(AppSettings(), detect_gpu_now=False)
    bus = services.bus

    opened = bus.dispatch(OpenBoardCommand(args.board))
    if not opened.success:
        print(f"error: {opened.message}", file=sys.stderr)
        return EXIT_LOAD_FAILED

    summary = bus.dispatch(BoardSummaryCommand())
    session = services.project.session
    assert session is not None
    output: dict[str, Any] = dict(summary.data)
    output["warnings"] = [str(w) for w in session.load_result.warnings]
    output["timing_ms"] = {
        "read": round(session.load_result.stats.read_seconds * 1e3, 2),
        "parse": round(session.load_result.stats.parse_seconds * 1e3, 2),
        "build": round(session.load_result.stats.build_seconds * 1e3, 2),
    }
    output["sha256"] = session.source_sha256
    code = EXIT_OK
    if args.check_command is not None:
        checked = bus.dispatch(ValidateAICommand(args.check_command))
        output["command_check"] = {"valid": checked.success, "message": checked.message}
        code = EXIT_OK if checked.success else EXIT_COMMAND_INVALID
    print(json.dumps(output, indent=2))
    return code
