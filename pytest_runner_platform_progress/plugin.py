from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import pytest


PROGRESS_WRITE_INTERVAL_SECONDS = 0.5


@dataclass
class ProgressSnapshot:
    collected: int | None = None
    completed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    xfailed: int = 0
    xpassed: int = 0
    percent: float | None = None
    updated_at: str | None = None


class ProgressReporter:
    def __init__(self, path: Path):
        self.path = path
        self.collected: int | None = None
        self.completed: set[str] = set()
        self.reports: dict[str, list] = {}
        self.counts = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
        }
        self._last_write_at: float | None = None
        self._dirty = False

    def set_collected(self, count: int) -> None:
        if self.collected is None:
            self.collected = count
        else:
            self.collected = max(self.collected, count)
        self._dirty = True
        self.maybe_write(force=True)

    def add_report(self, report) -> None:
        self.reports.setdefault(report.nodeid, []).append(report)

    def finish(self, nodeid: str) -> None:
        if nodeid in self.completed:
            return
        self.completed.add(nodeid)
        outcome = self._classify(self.reports.pop(nodeid, []))
        self.counts[outcome] += 1
        self._dirty = True
        self.maybe_write()

    def _classify(self, reports: list) -> str:
        if any(report.failed and report.when in {"setup", "teardown"} for report in reports):
            return "errors"
        if any(report.failed and report.when == "call" for report in reports):
            return "failed"
        if any(getattr(report, "wasxfail", None) and report.passed for report in reports):
            return "xpassed"
        if any(getattr(report, "wasxfail", None) and report.skipped for report in reports):
            return "xfailed"
        if any(report.when == "call" and report.passed for report in reports):
            return "passed"
        if any(report.skipped for report in reports):
            return "skipped"
        return "errors"

    def snapshot(self) -> ProgressSnapshot:
        completed = len(self.completed)
        percent = None
        if self.collected:
            percent = round(min(completed / self.collected * 100, 100), 1)
        return ProgressSnapshot(
            collected=self.collected,
            completed=completed,
            passed=self.counts["passed"],
            failed=self.counts["failed"],
            skipped=self.counts["skipped"],
            errors=self.counts["errors"],
            xfailed=self.counts["xfailed"],
            xpassed=self.counts["xpassed"],
            percent=percent,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def maybe_write(self, force: bool = False) -> None:
        if force:
            if self._dirty or self._last_write_at is None:
                self.write()
            return
        if self._last_write_at is None or monotonic() - self._last_write_at >= PROGRESS_WRITE_INTERVAL_SECONDS:
            self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(asdict(self.snapshot()), ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.path)
        self._last_write_at = monotonic()
        self._dirty = False


_REPORTER: ProgressReporter | None = None


def pytest_configure(config):
    global _REPORTER
    path = os.getenv("PYTEST_RUNNER_PROGRESS_PATH")
    if not path or hasattr(config, "workerinput"):
        _REPORTER = None
        return
    _REPORTER = ProgressReporter(Path(path))
    _REPORTER.write()


def pytest_collection_finish(session):
    if _REPORTER:
        _REPORTER.set_collected(len(session.items))


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    if _REPORTER:
        _REPORTER.set_collected(len(ids))


def pytest_runtest_logreport(report):
    if _REPORTER:
        _REPORTER.add_report(report)


def pytest_runtest_logfinish(nodeid, location):
    if _REPORTER:
        _REPORTER.finish(nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _REPORTER:
        _REPORTER.maybe_write(force=True)


def pytest_unconfigure(config):
    if _REPORTER:
        _REPORTER.maybe_write(force=True)
