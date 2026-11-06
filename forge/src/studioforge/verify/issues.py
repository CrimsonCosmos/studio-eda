"""Shared issue model for ALL verification (ERC, simulation, datasheet rules, EE rules, import review).

Every finding has a stable rule id, a severity, a location in the design, a human message, optional
numbers (measured vs limit, with units) and a citation (datasheet page / standard / simulation name).
Rules are the unit of measurement for false-positive/false-negative tracking in the corpus."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    ERROR = "error"          # design will not work / is unsafe
    WARNING = "warning"      # likely problem or unverifiable assumption
    INFO = "info"            # note
    PASS = "pass"            # a check that ran and succeeded (kept so reports show coverage, not just failures)


@dataclass
class Issue:
    rule: str                              # stable id, e.g. "buck.vout_error", "sim.ldo.dropout", "ee.i2c_pullup_missing"
    severity: Severity
    message: str
    where: str = ""                        # "U3", "net +3V3", "block joint.buck12", "U3.7"
    measured: float | None = None
    limit: float | None = None
    unit: str = ""
    citation: str = ""                     # "TPS54360B datasheet Eq.44", "IPC-2221", "ngspice: tran buck_load_step"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)       # rule ids that actually ran (coverage)

    def add(self, issue: Issue) -> Issue:
        self.issues.append(issue)
        if issue.rule not in self.checked:
            self.checked.append(issue.rule)
        return issue

    def extend(self, other: "Report") -> None:
        for i in other.issues:
            self.add(i)
        for r in other.checked:
            if r not in self.checked:
                self.checked.append(r)

    def ran(self, rule: str) -> None:
        if rule not in self.checked:
            self.checked.append(rule)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        n = {s: sum(1 for i in self.issues if i.severity == s) for s in Severity}
        return f"{n[Severity.ERROR]} errors, {n[Severity.WARNING]} warnings, {n[Severity.PASS]} passed, {len(self.checked)} rules ran"

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "checked": self.checked, "issues": [i.to_dict() for i in self.issues]}
