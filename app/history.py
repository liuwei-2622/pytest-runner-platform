from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import TestRun

COMPLETED_STATUSES = {"passed", "failed", "error", "timeout"}
FAILURE_STATUSES = {"failed", "error", "timeout"}


@dataclass(frozen=True)
class TrendPoint:
    run_id: str
    created_at: str
    status: str
    duration_seconds: float | None
    total: int
    failed: int
    errors: int
    skipped: int


@dataclass(frozen=True)
class HistorySummary:
    total_runs: int
    completed_runs: int
    pass_rate: float | None
    average_duration_seconds: float | None
    status_counts: dict[str, int]
    recent_failures: list[TestRun]
    trend_points: list[TrendPoint]


def _trend_point(run: TestRun) -> TrendPoint:
    return TrendPoint(
        run_id=run.id,
        created_at=run.created_at,
        status=run.status,
        duration_seconds=run.duration_seconds,
        total=run.progress.collected or run.progress.completed or 0,
        failed=run.progress.failed,
        errors=run.progress.errors,
        skipped=run.progress.skipped,
    )


def build_history_summary(runs: list[TestRun], limit: int = 30) -> HistorySummary:
    completed = [run for run in runs if run.status in COMPLETED_STATUSES]
    passed = sum(1 for run in completed if run.status == "passed")
    durations = [run.duration_seconds for run in completed if run.duration_seconds is not None]

    pass_rate = round(passed / len(completed) * 100, 1) if completed else None
    average_duration = round(sum(durations) / len(durations), 2) if durations else None
    recent_failures = [run for run in runs if run.status in FAILURE_STATUSES][:5]
    trend_points = [_trend_point(run) for run in reversed(runs[:limit])]

    return HistorySummary(
        total_runs=len(runs),
        completed_runs=len(completed),
        pass_rate=pass_rate,
        average_duration_seconds=average_duration,
        status_counts=dict(Counter(run.status for run in runs)),
        recent_failures=recent_failures,
        trend_points=trend_points,
    )
