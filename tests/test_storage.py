import json
import sqlite3
from pathlib import Path

from app import storage
from app.models import RunOptions, RunProgress, TestRun as _TestRun, utc_now


def isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(storage, "RUN_METADATA_DB", tmp_path / "runs.sqlite3")
    storage._initialized_storage.clear()


def legacy_run(tmp_path: Path, run_id: str = "legacy001") -> _TestRun:
    report_dir = tmp_path / "reports" / run_id
    return _TestRun(
        id=run_id,
        status="passed",
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:01+00:00",
        finished_at="2026-01-01T00:00:03+00:00",
        test_path="tests/test_legacy.py",
        resolved_test_path=str(tmp_path / "tests" / "test_legacy.py"),
        options=RunOptions(keyword="legacy", workers="2", last_failed=True),
        return_code=0,
        command=["python", "-m", "pytest", "tests/test_legacy.py"],
        report_dir=str(report_dir),
        html_report_path=str(report_dir / "pytest.html"),
        junit_report_path=str(report_dir / "junit.xml"),
        stdout_path=str(report_dir / "stdout.log"),
        stderr_path=str(report_dir / "stderr.log"),
        allure_results_path=str(report_dir / "allure-results"),
        allure_report_path=str(report_dir / "allure-report"),
        project_id="demo",
        project_name="Demo",
        progress=RunProgress(collected=3, completed=3, passed=3, percent=100.0),
    )


def write_legacy_metadata(run: _TestRun) -> None:
    path = Path(run.report_dir) / "metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run.to_dict(), ensure_ascii=False), encoding="utf-8")


def test_create_run_persists_to_sqlite_without_new_metadata_json(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions(keyword="smoke"))

    assert Path(run.report_dir).is_dir()
    assert not (Path(run.report_dir) / "metadata.json").exists()
    assert storage.RUN_METADATA_DB.exists()
    with sqlite3.connect(storage.RUN_METADATA_DB) as conn:
        row = conn.execute("SELECT id, status, project_id FROM runs WHERE id = ?", (run.id,)).fetchone()
    assert row == (run.id, "queued", "demo")


def test_get_update_and_progress_round_trip_sqlite_metadata(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run(
        "demo",
        "Demo",
        "tests/test_api.py",
        tmp_path / "tests" / "test_api.py",
        RunOptions(keyword="api", marker="smoke", workers="auto", env_vars={"TOKEN": "secret"}),
    )

    started_at = utc_now()
    finished_at = utc_now()
    storage.update_run(
        run.id,
        status="failed",
        started_at=started_at,
        finished_at=finished_at,
        return_code=1,
        command=["python", "-m", "pytest", "tests/test_api.py", "-k", "api"],
        error_message="failed cases",
    )
    storage.update_run_progress(
        run.id,
        RunProgress(collected=4, completed=3, passed=2, failed=1, percent=75.0, updated_at=utc_now()),
    )

    loaded = storage.get_run(run.id)

    assert loaded.status == "failed"
    assert loaded.started_at == started_at
    assert loaded.finished_at == finished_at
    assert loaded.return_code == 1
    assert loaded.command == ["python", "-m", "pytest", "tests/test_api.py", "-k", "api"]
    assert loaded.error_message == "failed cases"
    assert loaded.options.keyword == "api"
    assert loaded.options.env_vars == {"TOKEN": "secret"}
    assert loaded.progress.collected == 4
    assert loaded.progress.failed == 1
    assert loaded.progress.percent == 75.0
    assert loaded.project_id == "demo"
    assert loaded.html_report_path.endswith("pytest.html")


def test_list_runs_returns_newest_first_from_sqlite(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    older = storage.create_run("demo", "Demo", "older", tmp_path / "older", RunOptions())
    newer = storage.create_run("demo", "Demo", "newer", tmp_path / "newer", RunOptions())
    storage.update_run(older.id, created_at="2026-01-01T00:00:00+00:00")
    storage.update_run(newer.id, created_at="2026-01-02T00:00:00+00:00")

    assert [run.id for run in storage.list_runs()] == [newer.id, older.id]


def test_count_runs_and_paginated_list_runs_use_sqlite_order(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    first = storage.create_run("demo", "Demo", "first", tmp_path / "first", RunOptions())
    second = storage.create_run("demo", "Demo", "second", tmp_path / "second", RunOptions())
    third = storage.create_run("demo", "Demo", "third", tmp_path / "third", RunOptions())
    storage.update_run(first.id, created_at="2026-01-01T00:00:00+00:00")
    storage.update_run(second.id, created_at="2026-01-02T00:00:00+00:00")
    storage.update_run(third.id, created_at="2026-01-03T00:00:00+00:00")

    assert storage.count_runs() == 3
    assert [run.id for run in storage.list_runs(limit=2, offset=1)] == [second.id, first.id]
    assert [run.id for run in storage.list_runs()] == [third.id, second.id, first.id]


def test_legacy_metadata_is_backfilled_idempotently_and_corrupt_json_is_skipped(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    legacy = legacy_run(tmp_path)
    write_legacy_metadata(legacy)
    corrupt_path = tmp_path / "reports" / "bad" / "metadata.json"
    corrupt_path.parent.mkdir(parents=True)
    corrupt_path.write_text("{bad json", encoding="utf-8")

    storage.ensure_storage()
    storage._initialized_storage.clear()
    storage.ensure_storage()

    runs = storage.list_runs()

    assert [run.id for run in runs] == [legacy.id]
    assert runs[0].options.keyword == "legacy"
    with sqlite3.connect(storage.RUN_METADATA_DB) as conn:
        count = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    assert count == 1


def test_artifact_path_and_log_preview_remain_disk_backed(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    html_path = Path(run.html_report_path)
    stdout_path = Path(run.stdout_path)
    html_path.write_text("<html>ok</html>", encoding="utf-8")
    stdout_path.write_text("first line\nsecond line", encoding="utf-8")

    assert storage.artifact_path(run.id, "pytest.html") == html_path
    assert storage.artifact_path(run.id, "junit.xml") is None
    assert storage.read_log_preview(run.stdout_path) == "first line\nsecond line"
