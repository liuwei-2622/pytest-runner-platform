from app.history import build_history_summary
from app.models import RunOptions, RunProgress, TestRun as _TestRun


def make_run(run_id, status, created_at, started_at=None, finished_at=None, progress=None):
    return _TestRun(
        id=run_id,
        status=status,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        test_path="tests",
        resolved_test_path="/tmp/tests",
        options=RunOptions(),
        return_code=0,
        command=[],
        report_dir="/tmp/report",
        html_report_path="/tmp/report/pytest.html",
        junit_report_path="/tmp/report/junit.xml",
        stdout_path="/tmp/report/stdout.log",
        stderr_path="/tmp/report/stderr.log",
        project_id="demo",
        project_name="Demo",
        progress=progress or RunProgress(),
    )


def test_build_history_summary_counts_and_rates():
    runs = [
        make_run("3", "failed", "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:05+00:00"),
        make_run("2", "passed", "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:03+00:00"),
        make_run("1", "queued", "2026-01-01T00:00:00+00:00"),
    ]

    summary = build_history_summary(runs)

    assert summary.total_runs == 3
    assert summary.completed_runs == 2
    assert summary.pass_rate == 50.0
    assert summary.average_duration_seconds == 4.0
    assert summary.status_counts == {"failed": 1, "passed": 1, "queued": 1}
    assert [run.id for run in summary.recent_failures] == ["3"]


def test_build_history_summary_orders_trends_oldest_to_newest():
    runs = [
        make_run("3", "passed", "2026-01-03T00:00:00+00:00", progress=RunProgress(collected=3, failed=0)),
        make_run("2", "error", "2026-01-02T00:00:00+00:00", progress=RunProgress(completed=2, errors=1)),
        make_run("1", "failed", "2026-01-01T00:00:00+00:00", progress=RunProgress(collected=1, failed=1)),
    ]

    summary = build_history_summary(runs, limit=2)

    assert [point.run_id for point in summary.trend_points] == ["2", "3"]
    assert summary.trend_points[0].total == 2
    assert summary.trend_points[0].errors == 1
    assert summary.trend_points[1].total == 3
