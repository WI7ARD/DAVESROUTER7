"""Internal geometry-check result."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pcbrouter.drc.violation import DRCViolation, Severity

#: Name used everywhere in UI and docs. This is NOT KiCad DRC.
CHECK_NAME = "Internal Geometry Check"


class DRCStatus(Enum):
    PASS = "PASS"
    WARNINGS = "WARNINGS"
    FAIL = "FAIL"


@dataclass
class DRCResult:
    violations: list[DRCViolation] = field(default_factory=list)
    check_count: int = 0
    elapsed_time: float = 0.0
    board_fingerprint: str = ""
    rules_digest: str = ""
    #: Checks that could not run, e.g. {"clearance rule unknown": 42}.
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[DRCViolation]:
        return [v for v in self.violations if v.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[DRCViolation]:
        return [v for v in self.violations if v.severity is Severity.WARNING]

    @property
    def infos(self) -> list[DRCViolation]:
        return [v for v in self.violations if v.severity is Severity.INFO]

    @property
    def status(self) -> DRCStatus:
        if self.errors:
            return DRCStatus.FAIL
        if self.warnings:
            return DRCStatus.WARNINGS
        return DRCStatus.PASS

    def summary(self) -> str:
        return (
            f"{CHECK_NAME}: {self.status.value} — errors {len(self.errors)}, warnings "
            f"{len(self.warnings)}, info {len(self.infos)}, checks {self.check_count:,}, "
            f"{self.elapsed_time:.2f} s"
        )
