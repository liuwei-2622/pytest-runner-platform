from pathlib import Path

from fastapi.testclient import TestClient

from app import main, storage
from app.models import RunOptions, utc_now


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
