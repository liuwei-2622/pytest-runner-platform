from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from .models import TestRun

Outcome = Literal["passed", "failed", "error", "skipped"]


@dataclass(frozen=True)
class TestCaseResult:
    name: str
    classname: str
    file: str
    line: str
    time_seconds: float
    outcome: Outcome
    message: str
    details: str


@dataclass(frozen=True)
class TestReportSummary:
    exists: bool
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    time_seconds: float = 0.0
    cases: list[TestCaseResult] = field(default_factory=list)
    failed_cases: list[TestCaseResult] = field(default_factory=list)
    skipped_cases: list[TestCaseResult] = field(default_factory=list)
    error_message: str = ""


def _float_attr(element: ElementTree.Element, name: str) -> float:
    try:
        return float(element.attrib.get(name, "0") or 0)
    except ValueError:
        return 0.0


def _case_outcome(testcase: ElementTree.Element) -> tuple[Outcome, str, str]:
    for tag, outcome in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        child = testcase.find(tag)
        if child is not None:
            return outcome, child.attrib.get("message", ""), (child.text or "").strip()
    return "passed", "", ""


def _testcases(root: ElementTree.Element) -> list[ElementTree.Element]:
    if root.tag == "testcase":
        return [root]
    return list(root.iter("testcase"))


def _root_time(root: ElementTree.Element, cases: list[TestCaseResult]) -> float:
    if root.tag in {"testsuite", "testsuites"} and "time" in root.attrib:
        return round(_float_attr(root, "time"), 3)
    return round(sum(case.time_seconds for case in cases), 3)


def parse_junit_report(path: str | Path) -> TestReportSummary:
    report_path = Path(path)
    if not report_path.exists():
        return TestReportSummary(exists=False)

    try:
        root = ElementTree.parse(report_path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        return TestReportSummary(exists=True, error_message=f"JUnit XML 解析失败: {exc}")

    cases: list[TestCaseResult] = []
    for testcase in _testcases(root):
        outcome, message, details = _case_outcome(testcase)
        cases.append(
            TestCaseResult(
                name=testcase.attrib.get("name", ""),
                classname=testcase.attrib.get("classname", ""),
                file=testcase.attrib.get("file", ""),
                line=testcase.attrib.get("line", ""),
                time_seconds=round(_float_attr(testcase, "time"), 3),
                outcome=outcome,
                message=message,
                details=details,
            )
        )

    failed_cases = [case for case in cases if case.outcome in {"failed", "error"}]
    skipped_cases = [case for case in cases if case.outcome == "skipped"]
    failed = sum(1 for case in cases if case.outcome == "failed")
    errors = sum(1 for case in cases if case.outcome == "error")
    skipped = len(skipped_cases)
    total = len(cases)

    return TestReportSummary(
        exists=True,
        total=total,
        passed=total - failed - errors - skipped,
        failed=failed,
        errors=errors,
        skipped=skipped,
        time_seconds=_root_time(root, cases),
        cases=cases,
        failed_cases=failed_cases,
        skipped_cases=skipped_cases,
    )


def report_for_run(run: TestRun) -> TestReportSummary:
    return parse_junit_report(run.junit_report_path)
