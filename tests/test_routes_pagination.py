import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, storage
from app.models import RunOptions, RunProgress, utc_now


def isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(storage, "RUN_METADATA_DB", tmp_path / "runs.sqlite3")
    storage._initialized_storage.clear()
    monkeypatch.setattr(main, "recover_stale_runs", lambda: 0)


def make_completed_run(tmp_path, project_id="demo", test_path="tests"):
    run = storage.create_run(project_id, "Demo", test_path, tmp_path / test_path, RunOptions())
    storage.update_run(run.id, status="passed", started_at=utc_now(), finished_at=utc_now(), return_code=0)
    return storage.get_run(run.id)


def test_runs_api_returns_requested_page_and_pagination_metadata(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    first = make_completed_run(tmp_path, test_path="first")
    second = make_completed_run(tmp_path, test_path="second")
    third = make_completed_run(tmp_path, test_path="third")
    storage.update_run(first.id, created_at="2026-01-01T00:00:00+00:00")
    storage.update_run(second.id, created_at="2026-01-02T00:00:00+00:00")
    storage.update_run(third.id, created_at="2026-01-03T00:00:00+00:00")

    response = TestClient(main.app).get("/api/runs?page=2&page_size=2")

    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["runs"]] == [first.id]
    assert data["pagination"] == {
        "page": 2,
        "page_size": 2,
        "total": 3,
        "total_pages": 2,
        "offset": 2,
        "has_prev": True,
        "has_next": False,
        "prev_page": 1,
        "next_page": None,
    }
    assert data["history"]["total_runs"] == 3


def test_runs_page_renders_pagination_controls(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    first = make_completed_run(tmp_path, test_path="first")
    second = make_completed_run(tmp_path, test_path="second")
    third = make_completed_run(tmp_path, test_path="third")
    storage.update_run(first.id, created_at="2026-01-01T00:00:00+00:00")
    storage.update_run(second.id, created_at="2026-01-02T00:00:00+00:00")
    storage.update_run(third.id, created_at="2026-01-03T00:00:00+00:00")

    response = TestClient(main.app).get("/runs?page=1&page_size=2")

    assert response.status_code == 200
    assert "第 1 / 2 页，共 3 条，每页 2 条" in response.text
    assert "href=\"/runs?page=2&page_size=2\"" in response.text


def write_junit_report(path: str) -> None:
    Path(path).write_text(
        """
        <testsuite tests="5" failures="3" skipped="2" time="1.5">
          <testcase classname="Suite" name="test_fail_1" time="0.1"><failure message="f1">detail 1</failure></testcase>
          <testcase classname="Suite" name="test_fail_2" time="0.2"><failure message="f2">detail 2</failure></testcase>
          <testcase classname="Suite" name="test_error" time="0.3"><error message="e1">detail e</error></testcase>
          <testcase classname="Suite" name="test_skip_1" time="0"><skipped message="s1" /></testcase>
          <testcase classname="Suite" name="test_skip_2" time="0"><skipped message="s2" /></testcase>
        </testsuite>
        """.strip(),
        encoding="utf-8",
    )


def test_report_api_paginates_failed_and_skipped_cases(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)
    write_junit_report(run.junit_report_path)

    response = TestClient(main.app).get(
        f"/api/runs/{run.id}/report?failed_page=2&failed_page_size=1&skipped_page=2&skipped_page_size=1"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert data["failed"] == 2
    assert data["errors"] == 1
    assert data["skipped"] == 2
    assert [case["name"] for case in data["failed_cases"]] == ["test_fail_2"]
    assert [case["name"] for case in data["skipped_cases"]] == ["test_skip_2"]
    assert data["failed_pagination"]["total"] == 3
    assert data["failed_pagination"]["page"] == 2
    assert data["skipped_pagination"]["total"] == 2
    assert data["skipped_pagination"]["page"] == 2


def test_run_detail_page_renders_paginated_report_cases(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)
    write_junit_report(run.junit_report_path)

    response = TestClient(main.app).get(
        f"/runs/{run.id}?failed_page=2&failed_page_size=1&skipped_page=2&skipped_page_size=1"
    )

    assert response.status_code == 200
    assert "test_fail_2" in response.text
    assert "test_fail_1" not in response.text
    assert "test_skip_2" in response.text
    assert "test_skip_1" not in response.text
    assert "第 2 / 3 页，共 3 条" in response.text
    assert "第 2 / 2 页，共 2 条" in response.text


def test_run_status_api_returns_phase_and_live_log_previews(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    Path(run.stderr_path).write_text("pytest setup is still running", encoding="utf-8")
    storage.update_run(run.id, status="running", started_at=utc_now(), progress=RunProgress(updated_at=utc_now()))

    response = TestClient(main.app).get(f"/api/runs/{run.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["phase_text"] == "pytest 启动/环境准备中（已有日志输出）"
    assert data["stderr_preview"] == "pytest setup is still running"
    assert data["is_active"] is True


def test_cancel_run_api_marks_untracked_active_run_cancelled(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    main._RUN_TASKS.clear()
    run = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    storage.update_run(run.id, status="running", started_at=utc_now())

    response = TestClient(main.app).post(f"/api/runs/{run.id}/cancel")
    loaded = storage.get_run(run.id)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert loaded.status == "error"
    assert loaded.finished_at is not None
    assert loaded.return_code == -15
    assert loaded.error_message == "用户取消运行"


def test_runs_delete_redirects_with_success_message_and_removes_disk(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)
    report_dir = Path(run.report_dir)
    (report_dir / "stdout.log").write_text("old log", encoding="utf-8")

    response = TestClient(main.app).post(
        "/runs/delete",
        data={"run_ids": [run.id], "page": "1", "page_size": "25"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs?page=1&page_size=25&message=")
    assert storage.get_run(run.id) is None
    assert not report_dir.exists()


def test_runs_delete_without_selection_redirects_with_message(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    response = TestClient(main.app).post(
        "/runs/delete",
        data={"page": "2", "page_size": "10"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs?page=2&page_size=10&message=")
    assert storage.count_runs() == 0


def test_runs_page_renders_delete_message_from_query(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    response = TestClient(main.app).get("/runs?message=已删除%201%20条运行记录。")

    assert response.status_code == 200
    assert "已删除 1 条运行记录。" in response.text


def test_runs_delete_filesystem_error_redirects_with_error_message(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    def fail_delete_runs(run_ids):
        raise OSError("permission denied")

    monkeypatch.setattr(main, "delete_runs", fail_delete_runs)
    client = TestClient(main.app)

    response = client.post(
        "/runs/delete",
        data={"run_ids": ["run-1"], "page": "3", "page_size": "50"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/runs?page=3&page_size=50&error=")
    assert "permission%20denied" in location

    redirected = client.get(location)
    assert redirected.status_code == 200
    assert "删除运行记录失败：permission denied" in redirected.text


def test_runs_delete_database_error_redirects_with_error_message(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    def fail_delete_runs(run_ids):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main, "delete_runs", fail_delete_runs)
    client = TestClient(main.app)

    response = client.post(
        "/runs/delete",
        data={"run_ids": ["run-1"], "page": "4", "page_size": "25"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/runs?page=4&page_size=25&error=")
    assert "database%20is%20locked" in location

    redirected = client.get(location)
    assert redirected.status_code == 200
    assert "删除运行记录失败：database is locked" in redirected.text


def test_runs_page_renders_bulk_delete_controls(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)

    response = TestClient(main.app).get("/runs?page=1&page_size=25")

    assert response.status_code == 200
    assert 'form id="bulk-delete-form" method="post" action="/runs/delete"' in response.text
    assert 'input type="checkbox" id="select-all-runs"' in response.text
    assert f'input type="checkbox" name="run_ids" value="{run.id}"' in response.text
    assert 'button type="submit" class="link-button danger-button"' in response.text
    assert "删除选中记录" in response.text
    assert "确认删除选中的运行记录及其报告/日志文件吗？" in response.text
