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

    storage.update_run(first.id, created_at="2026-01-04T00:00:00+00:00")
    storage.update_run(second.id, created_at="2026-01-04T00:00:00+00:00")
    assert [run.id for run in storage.list_runs(limit=2)] == sorted([first.id, second.id], reverse=True)


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


def test_existing_sqlite_schema_is_migrated_and_metadata_is_backfilled(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    report_dir = tmp_path / "reports" / "old001"
    report_dir.mkdir(parents=True)
    storage.RUN_METADATA_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(storage.RUN_METADATA_DB) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              project_id TEXT NOT NULL,
              project_name TEXT NOT NULL,
              test_path TEXT NOT NULL,
              resolved_test_path TEXT NOT NULL,
              return_code INTEGER,
              report_dir TEXT NOT NULL,
              html_report_path TEXT NOT NULL,
              junit_report_path TEXT NOT NULL,
              stdout_path TEXT NOT NULL,
              stderr_path TEXT NOT NULL,
              command_json TEXT NOT NULL DEFAULT '[]',
              options_json TEXT NOT NULL DEFAULT '{}',
              progress_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO runs (
              id, status, created_at, started_at, finished_at, project_id, project_name,
              test_path, resolved_test_path, return_code, report_dir, html_report_path,
              junit_report_path, stdout_path, stderr_path, command_json, options_json,
              progress_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old001",
                "failed",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                "2026-01-01T00:00:02+00:00",
                "demo",
                "Demo",
                "tests/test_old.py",
                str(tmp_path / "tests" / "test_old.py"),
                1,
                str(report_dir),
                str(report_dir / "pytest.html"),
                str(report_dir / "junit.xml"),
                str(report_dir / "stdout.log"),
                str(report_dir / "stderr.log"),
                json.dumps(["python", "-m", "pytest", "tests/test_old.py"]),
                json.dumps({"keyword": "old", "workers": "auto"}),
                json.dumps({"collected": 2, "completed": 2, "failed": 1, "percent": 100.0}),
                "2026-01-01T00:00:02+00:00",
            ),
        )

    storage.ensure_storage()
    loaded = storage.get_run("old001")

    with sqlite3.connect(storage.RUN_METADATA_DB) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        row = conn.execute("SELECT metadata_json FROM runs WHERE id = 'old001'").fetchone()

    assert {"allure_results_path", "allure_report_path", "error_message", "metadata_json"} <= columns
    assert row[0] != "{}"
    assert loaded.status == "failed"
    assert loaded.command == ["python", "-m", "pytest", "tests/test_old.py"]
    assert loaded.options.keyword == "old"
    assert loaded.options.workers == "auto"
    assert loaded.progress.failed == 1
    assert loaded.allure_results_path.endswith("allure-results")
    assert loaded.allure_report_path.endswith("allure-report")
    assert loaded.error_message == ""


def test_minimal_existing_sqlite_schema_migrates_before_creating_indexes(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    storage.RUN_METADATA_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(storage.RUN_METADATA_DB) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (id TEXT PRIMARY KEY);
            INSERT INTO runs (id) VALUES ('minimal001');
            """
        )

    storage.ensure_storage()
    loaded = storage.get_run("minimal001")

    with sqlite3.connect(storage.RUN_METADATA_DB) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(runs)")}

    assert {"status", "created_at", "metadata_json"} <= columns
    assert {"idx_runs_created_at", "idx_runs_status"} <= indexes
    assert loaded.id == "minimal001"
    assert loaded.status == "queued"


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


def test_delete_runs_removes_completed_run_from_sqlite_and_disk(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    report_dir = Path(run.report_dir)
    (report_dir / "stdout.log").write_text("log", encoding="utf-8")
    (report_dir / "allure-results").mkdir()
    (report_dir / "allure-results" / "result.json").write_text("{}", encoding="utf-8")
    storage.update_run(run.id, status="passed", return_code=0, finished_at=utc_now())

    result = storage.delete_runs([run.id])

    assert result.deleted == 1
    assert result.skipped_active == 0
    assert result.missing == 0
    assert storage.get_run(run.id) is None
    assert not report_dir.exists()
    assert storage.count_runs() == 0


def test_delete_runs_skips_active_runs_and_leaves_disk_intact(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    report_dir = Path(run.report_dir)
    (report_dir / "stdout.log").write_text("still running", encoding="utf-8")
    storage.update_run(run.id, status="running", started_at=utc_now())

    result = storage.delete_runs([run.id])

    assert result.deleted == 0
    assert result.skipped_active == 1
    assert result.missing == 0
    assert storage.get_run(run.id) is not None
    assert report_dir.exists()
    assert (report_dir / "stdout.log").read_text(encoding="utf-8") == "still running"


def test_delete_runs_counts_missing_ids(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    result = storage.delete_runs(["missing001"])

    assert result.deleted == 0
    assert result.skipped_active == 0
    assert result.missing == 1


def test_delete_runs_refuses_report_dir_outside_reports_root(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    outside_dir = tmp_path / "outside-report"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_text("do not delete", encoding="utf-8")
    storage.update_run(run.id, status="passed", finished_at=utc_now(), report_dir=str(outside_dir))

    try:
        storage.delete_runs([run.id])
    except ValueError as exc:
        assert "outside reports directory" in str(exc)
    else:
        raise AssertionError("delete_runs should reject report_dir outside REPORTS_DIR")

    assert storage.get_run(run.id) is not None
    assert outside_dir.exists()
    assert (outside_dir / "keep.txt").exists()


def test_delete_runs_refuses_reports_root_as_report_dir(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    reports_root = storage.REPORTS_DIR
    sentinel = reports_root / "sentinel.txt"
    sentinel.write_text("keep root", encoding="utf-8")
    storage.update_run(run.id, status="passed", finished_at=utc_now(), report_dir=str(reports_root))

    try:
        storage.delete_runs([run.id])
    except ValueError as exc:
        assert "outside reports directory" in str(exc)
    else:
        raise AssertionError("delete_runs should reject REPORTS_DIR as report_dir")

    assert storage.get_run(run.id) is not None
    assert reports_root.exists()
    assert sentinel.exists()


def test_delete_runs_keeps_row_and_report_dir_when_rmtree_fails(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    report_dir = Path(run.report_dir)
    (report_dir / "stdout.log").write_text("retry later", encoding="utf-8")
    storage.update_run(run.id, status="passed", finished_at=utc_now())

    def fail_rmtree(path):
        raise OSError("permission denied")

    monkeypatch.setattr(storage.shutil, "rmtree", fail_rmtree)

    try:
        storage.delete_runs([run.id])
    except OSError as exc:
        assert str(exc) == "permission denied"
    else:
        raise AssertionError("delete_runs should surface rmtree failures")

    assert storage.get_run(run.id) is not None
    assert report_dir.exists()
    assert (report_dir / "stdout.log").exists()


def test_delete_runs_deduplicates_submitted_run_ids(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    storage.update_run(run.id, status="passed", finished_at=utc_now())

    result = storage.delete_runs([run.id, run.id, "missing001", "missing001"])

    assert result.deleted == 1
    assert result.skipped_active == 0
    assert result.missing == 1
    assert storage.get_run(run.id) is None

