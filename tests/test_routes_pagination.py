import asyncio
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, storage
from app.models import RunOptions, RunProgress, utc_now
from app.projects import ProjectConfig


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
    assert "href=\"/runs?page=2&amp;page_size=2\"" in response.text


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
    assert f'action="/runs/{run.id}/failed-cases/1/rerun"' in response.text
    assert "重跑" in response.text


def test_run_detail_and_status_api_redact_env_secret_values(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = storage.create_run(
        "demo",
        "Demo",
        "tests",
        tmp_path / "tests",
        RunOptions(env_vars={"TOKEN": "run-secret"}),
    )
    project = ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(tmp_path),
        python_executable="/usr/bin/python3",
        working_directory=str(tmp_path),
        allowed_test_roots=[str(tmp_path)],
        default_env={"DEFAULT_TOKEN": "project-secret"},
    )
    Path(run.stdout_path).write_text("run-secret project-secret TOKEN=abc123", encoding="utf-8")
    Path(run.stderr_path).write_text("Authorization: Bearer bearer-secret", encoding="utf-8")
    storage.update_run(run.id, status="running", started_at=utc_now(), progress=RunProgress(updated_at=utc_now()))
    monkeypatch.setattr(main, "get_project", lambda project_id: project)

    detail = TestClient(main.app).get(f"/runs/{run.id}")
    status = TestClient(main.app).get(f"/api/runs/{run.id}")

    assert detail.status_code == 200
    assert "run-secret" not in detail.text
    assert "project-secret" not in detail.text
    assert "TOKEN=******" in detail.text
    data = status.json()
    assert "run-secret" not in data["stdout_preview"]
    assert "project-secret" not in data["stdout_preview"]
    assert "bearer-secret" not in data["stderr_preview"]
    assert "Authorization: Bearer ******" in data["stderr_preview"]


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


def test_runs_delete_uses_thread_for_storage_deletion(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)
    calls = []

    async def fake_to_thread(func, *args):
        calls.append((func, args))
        return storage.DeleteRunsResult(deleted=1)

    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)

    response = TestClient(main.app).post(
        "/runs/delete",
        data={"run_ids": [run.id], "page": "1", "page_size": "25"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert calls == [(main.delete_runs, ([run.id],))]


def test_runs_delete_malformed_pagination_redirects_with_defaults(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    response = TestClient(main.app).post(
        "/runs/delete",
        data={"page": "abc", "page_size": "huge"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs?page=1&page_size=25&message=")


def test_runs_delete_clamps_out_of_range_pagination(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)

    response = TestClient(main.app).post(
        "/runs/delete",
        data={"run_ids": [run.id], "page": "-4", "page_size": "999"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs?page=1&page_size=100&message=")


def test_runs_delete_message_includes_all_partial_result_counts(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    def fake_delete_runs(run_ids):
        return storage.DeleteRunsResult(
            deleted=1,
            skipped_active=1,
            missing=1,
            skipped_invalid_report_dir=1,
            artifact_delete_failed=1,
        )

    monkeypatch.setattr(main, "delete_runs", fake_delete_runs)
    client = TestClient(main.app)

    response = client.post(
        "/runs/delete",
        data={"run_ids": ["a", "b", "c", "d"], "page": "1", "page_size": "25"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    redirected = client.get(response.headers["location"])
    assert "已删除 1 条运行记录" in redirected.text
    assert "跳过 1 条运行中记录" in redirected.text
    assert "忽略 1 条不存在记录" in redirected.text
    assert "跳过 1 条报告目录异常记录" in redirected.text
    assert "1 条报告目录清理失败" in redirected.text


def test_runs_page_renders_bulk_delete_controls(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)

    response = TestClient(main.app).get("/runs?page=1&page_size=25")

    assert response.status_code == 200
    assert 'form id="bulk-delete-form" method="post" action="/runs/delete"' in response.text
    assert 'input type="checkbox" id="select-all-runs"' in response.text
    assert f'input form="bulk-delete-form" type="checkbox" name="run_ids" value="{run.id}"' in response.text
    assert 'button form="bulk-delete-form" type="submit" class="link-button danger-button"' in response.text
    assert "删除选中记录" in response.text
    assert "确认删除选中的运行记录及其报告/日志文件吗？" in response.text


def test_runs_page_uses_sql_history_summary_without_full_run_scan(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)
    original_list_runs = storage.list_runs
    calls = []

    def paginated_only_list_runs(limit=None, offset=0, filters=None):
        calls.append((limit, offset, filters))
        assert limit is not None
        return original_list_runs(limit=limit, offset=offset, filters=filters)

    monkeypatch.setattr(main, "list_runs", paginated_only_list_runs)

    response = TestClient(main.app).get("/runs?page=1&page_size=25")

    assert response.status_code == 200
    assert f"value=\"{run.id}\"" in response.text
    assert [(limit, offset) for limit, offset, _filters in calls] == [(25, 0)]


def test_runs_api_uses_sql_history_summary_without_full_run_scan(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)
    original_list_runs = storage.list_runs
    calls = []

    def paginated_only_list_runs(limit=None, offset=0, filters=None):
        calls.append((limit, offset, filters))
        assert limit is not None
        return original_list_runs(limit=limit, offset=offset, filters=filters)

    monkeypatch.setattr(main, "list_runs", paginated_only_list_runs)

    response = TestClient(main.app).get("/api/runs?page=1&page_size=25")

    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["runs"]] == [run.id]
    assert data["history"]["total_runs"] == 1
    assert [(limit, offset) for limit, offset, _filters in calls] == [(25, 0)]


def test_runs_api_filters_by_project_status_and_path(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    first = make_completed_run(tmp_path, project_id="demo", test_path="tests/api/test_auth.py")
    second = make_completed_run(tmp_path, project_id="demo", test_path="tests/ui/test_auth.py")
    third = make_completed_run(tmp_path, project_id="other", test_path="tests/api/test_auth.py")
    storage.update_run(first.id, status="failed", created_at="2026-01-03T00:00:00+00:00")
    storage.update_run(second.id, status="failed", created_at="2026-01-02T00:00:00+00:00")
    storage.update_run(third.id, status="failed", created_at="2026-01-01T00:00:00+00:00")

    response = TestClient(main.app).get("/api/runs?project_id=demo&status=failed&test_path=api&page=1&page_size=25")

    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["runs"]] == [first.id]
    assert data["pagination"]["total"] == 1
    assert data["history"]["total_runs"] == 1
    assert data["history"]["status_counts"] == {"failed": 1}


def test_runs_page_renders_filters_and_preserves_them_in_pagination(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    first = make_completed_run(tmp_path, project_id="demo", test_path="tests/api/test_auth.py")
    second = make_completed_run(tmp_path, project_id="demo", test_path="tests/api/test_checkout.py")
    third = make_completed_run(tmp_path, project_id="demo", test_path="tests/ui/test_auth.py")
    storage.update_run(first.id, status="failed", created_at="2026-01-03T00:00:00+00:00")
    storage.update_run(second.id, status="failed", created_at="2026-01-02T00:00:00+00:00")
    storage.update_run(third.id, status="passed", created_at="2026-01-01T00:00:00+00:00")

    response = TestClient(main.app).get("/runs?project_id=demo&status=failed&test_path=api&page=1&page_size=1")

    assert response.status_code == 200
    assert "筛选" in response.text
    assert 'name="project_id"' in response.text
    assert 'option value="demo" selected' in response.text
    assert 'option value="failed" selected' in response.text
    assert 'name="test_path" value="api"' in response.text
    assert f'value="{first.id}"' in response.text
    assert f'value="{second.id}"' not in response.text
    assert f'value="{third.id}"' not in response.text
    assert "project_id=demo" in response.text
    assert "status=failed" in response.text
    assert "test_path=api" in response.text
    assert "page=2" in response.text


def test_run_pages_render_rerun_controls(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    run = make_completed_run(tmp_path)

    detail = TestClient(main.app).get(f"/runs/{run.id}")
    listing = TestClient(main.app).get("/runs?page=1&page_size=25")

    assert detail.status_code == 200
    assert f'action="/runs/{run.id}/rerun"' in detail.text
    assert "重跑" in detail.text
    assert listing.status_code == 200
    assert f'action="/runs/{run.id}/rerun"' in listing.text


def test_rerun_creates_new_run_from_source_options(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    project_root = tmp_path / "project"
    tests_dir = project_root / "tests"
    tests_dir.mkdir(parents=True)
    test_file = tests_dir / "test_sample.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    project = ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(project_root),
        python_executable="/usr/bin/python3",
        working_directory=str(project_root),
        allowed_test_roots=[str(tests_dir)],
    )
    source = storage.create_run(
        "demo",
        "Old Demo Name",
        "tests/test_sample.py",
        test_file,
        RunOptions(keyword="ok", marker="smoke", verbosity="verbose", maxfail=2, workers="2", env_vars={"TOKEN": "secret"}, last_failed=True, failed_first=True, tb="short"),
    )
    storage.update_run(source.id, status="failed", command=["old"], return_code=1, finished_at=utc_now())
    scheduled = []
    monkeypatch.setattr(main, "get_project", lambda project_id: project if project_id == "demo" else None)
    monkeypatch.setattr(main, "_schedule_run_task", lambda run_id: scheduled.append(run_id))

    response = TestClient(main.app).post(f"/runs/{source.id}/rerun", follow_redirects=False)

    assert response.status_code == 303
    new_run_id = response.headers["location"].removeprefix("/runs/")
    assert new_run_id != source.id
    assert scheduled == [new_run_id]
    rerun = storage.get_run(new_run_id)
    assert rerun.status == "queued"
    assert rerun.project_id == "demo"
    assert rerun.project_name == "Demo"
    assert rerun.test_path == "tests/test_sample.py"
    assert rerun.resolved_test_path == str(test_file.resolve())
    assert rerun.options.keyword == "ok"
    assert rerun.options.marker == "smoke"
    assert rerun.options.maxfail == 2
    assert rerun.options.env_vars == {"TOKEN": "secret"}
    assert rerun.command == []
    assert rerun.return_code is None
    assert rerun.progress.completed == 0
    assert rerun.report_dir != source.report_dir


def test_failed_case_rerun_creates_new_run_for_case_target(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    project_root = tmp_path / "project"
    tests_dir = project_root / "tests"
    tests_dir.mkdir(parents=True)
    test_file = tests_dir / "test_sample.py"
    test_file.write_text("def test_fail():\n    assert False\n", encoding="utf-8")
    project = ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(project_root),
        python_executable="/usr/bin/python3",
        working_directory=str(project_root),
        allowed_test_roots=[str(tests_dir)],
    )
    source = storage.create_run(
        "demo",
        "Demo",
        "tests",
        tests_dir,
        RunOptions(keyword="auth", env_vars={"TOKEN": "secret"}, last_failed=True, failed_first=True),
    )
    Path(source.junit_report_path).write_text(
        """
        <testsuite tests="1" failures="1">
          <testcase classname="tests.test_sample" name="test_fail" time="0.1">
            <failure message="failed">project/tests/test_sample.py:1: AssertionError</failure>
          </testcase>
        </testsuite>
        """.strip(),
        encoding="utf-8",
    )
    scheduled = []
    monkeypatch.setattr(main, "get_project", lambda project_id: project if project_id == "demo" else None)
    monkeypatch.setattr(main, "_schedule_run_task", lambda run_id: scheduled.append(run_id))

    response = TestClient(main.app).post(f"/runs/{source.id}/failed-cases/0/rerun", follow_redirects=False)

    assert response.status_code == 303
    new_run_id = response.headers["location"].removeprefix("/runs/")
    assert scheduled == [new_run_id]
    rerun = storage.get_run(new_run_id)
    assert rerun.test_path == "tests/test_sample.py::test_fail"
    assert rerun.resolved_test_path == f"{test_file.resolve()}::test_fail"
    assert rerun.options.keyword == "auth"
    assert rerun.options.env_vars == {"TOKEN": "secret"}
    assert rerun.options.last_failed is False
    assert rerun.options.failed_first is False
    assert rerun.report_dir != source.report_dir


def test_failed_case_rerun_rejects_missing_or_unusable_case(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    project_root = tmp_path / "project"
    tests_dir = project_root / "tests"
    tests_dir.mkdir(parents=True)
    project = ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(project_root),
        python_executable="/usr/bin/python3",
        working_directory=str(project_root),
        allowed_test_roots=[str(tests_dir)],
    )
    source = storage.create_run("demo", "Demo", "tests", tests_dir, RunOptions())
    Path(source.junit_report_path).write_text(
        """
        <testsuite tests="1" failures="1">
          <testcase classname="tests.test_sample" name="test_fail" time="0.1">
            <failure message="failed">details</failure>
          </testcase>
        </testsuite>
        """.strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "get_project", lambda project_id: project)

    missing = TestClient(main.app).post(f"/runs/{source.id}/failed-cases/2/rerun")
    unusable = TestClient(main.app).post(f"/runs/{source.id}/failed-cases/0/rerun")

    assert missing.status_code == 404
    assert unusable.status_code == 400
    assert "缺少可重跑" in unusable.text


def test_failed_case_rerun_missing_project_returns_400(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    source = make_completed_run(tmp_path, project_id="missing-project")
    write_junit_report(source.junit_report_path)
    monkeypatch.setattr(main, "get_project", lambda project_id: None)

    response = TestClient(main.app).post(f"/runs/{source.id}/failed-cases/0/rerun")

    assert response.status_code == 400
    assert "项目配置不存在" in response.text


def test_rerun_missing_source_returns_404(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    response = TestClient(main.app).post("/runs/missing/rerun")

    assert response.status_code == 404


def test_rerun_missing_project_returns_400(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)
    source = make_completed_run(tmp_path, project_id="missing-project")
    monkeypatch.setattr(main, "get_project", lambda project_id: None)

    response = TestClient(main.app).post(f"/runs/{source.id}/rerun")

    assert response.status_code == 400
    assert "项目配置不存在" in response.text


def test_startup_runs_retention_cleanup_after_recovery(monkeypatch):
    calls = []

    def fake_recover_stale_runs():
        calls.append("recover")
        return 0

    def fake_cleanup_runs_by_retention():
        calls.append("cleanup")
        return storage.DeleteRunsResult()

    monkeypatch.setattr(main, "recover_stale_runs", fake_recover_stale_runs)
    monkeypatch.setattr(main, "cleanup_runs_by_retention", fake_cleanup_runs_by_retention)

    asyncio.run(main.recover_stale_runs_on_startup())

    assert calls == ["recover", "cleanup"]
