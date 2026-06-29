import asyncio
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, projects
from app.config import COLLECT_TIMEOUT_SECONDS
from app.main import _project_default_env_text, _project_default_test_target
from app.projects import ProjectConfig, validate_project


def make_project(root: Path, allowed_roots: list[Path]) -> ProjectConfig:
    return ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(root),
        python_executable="python3",
        working_directory=str(root),
        allowed_test_roots=[str(path) for path in allowed_roots],
    )


def isolate_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_PATH", tmp_path / "projects.json")


def test_missing_projects_file_returns_no_projects(tmp_path, monkeypatch):
    isolate_projects(tmp_path, monkeypatch)

    assert projects.load_projects() == []
    assert projects.default_project_id() is None


def test_empty_projects_file_returns_no_projects(tmp_path, monkeypatch):
    isolate_projects(tmp_path, monkeypatch)
    projects.PROJECTS_PATH.write_text(json.dumps({"projects": []}), encoding="utf-8")

    assert projects.load_projects() == []
    assert projects.default_project_id() is None


def test_delete_project_allows_empty_project_config(tmp_path, monkeypatch):
    isolate_projects(tmp_path, monkeypatch)
    root = tmp_path / "project"
    root.mkdir()
    project = make_project(root, [root])
    projects.save_projects([project])

    projects.delete_project(project.id)

    assert projects.load_projects() == []
    assert json.loads(projects.PROJECTS_PATH.read_text(encoding="utf-8")) == {"projects": []}


def test_index_renders_add_project_guidance_when_no_projects(monkeypatch):
    monkeypatch.setattr(main, "list_projects", lambda: [])
    monkeypatch.setattr(main, "default_project_id", lambda: None)

    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    assert "先添加一个 pytest 项目" in response.text
    assert "Demo Project" not in response.text
    assert "id=\"run-form\"" not in response.text


def test_create_run_without_projects_returns_user_facing_error(monkeypatch):
    monkeypatch.setattr(main, "list_projects", lambda: [])
    monkeypatch.setattr(main, "default_project_id", lambda: None)

    response = TestClient(main.app).post("/runs", data={"test_path": "tests"})

    assert response.status_code == 400
    assert "请先添加 pytest 项目" in response.text
    assert "id=\"run-form\"" not in response.text


def test_project_default_test_target_uses_first_allowed_root_relative_to_project_root(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)

    assert _project_default_test_target(make_project(root, [tests])) == "tests"


def test_project_default_test_target_returns_dot_when_project_root_is_allowed(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    assert _project_default_test_target(make_project(root, [root])) == "."


def test_project_default_env_text_prefills_run_form_values(tmp_path):
    project = make_project(tmp_path, [tmp_path])
    project.default_env = {"API_URL": "https://example.test", "REGION_NAME": "demo"}

    assert _project_default_env_text(project) == "API_URL=https://example.test\nREGION_NAME=demo"


def test_project_config_defaults_collect_timeout_for_existing_data():
    project = ProjectConfig.from_dict(
        {
            "id": "demo",
            "name": "Demo",
            "root_path": "/tmp/demo",
            "python_executable": sys.executable,
            "working_directory": "/tmp/demo",
            "allowed_test_roots": ["/tmp/demo/tests"],
        }
    )

    assert project.collect_timeout_seconds == COLLECT_TIMEOUT_SECONDS


def test_project_config_persists_collect_timeout():
    project = ProjectConfig(
        id="demo",
        name="Demo",
        root_path="/tmp/demo",
        python_executable=sys.executable,
        working_directory="/tmp/demo",
        allowed_test_roots=["/tmp/demo/tests"],
        collect_timeout_seconds=99,
    )

    assert project.to_dict()["collect_timeout_seconds"] == 99


def test_validate_project_rejects_collect_timeout_outside_bounds(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    project = ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(root),
        python_executable=sys.executable,
        working_directory=str(root),
        allowed_test_roots=[str(tests)],
        collect_timeout_seconds=4,
    )

    try:
        validate_project(project)
    except ValueError as exc:
        assert "收集超时秒数" in str(exc)
    else:
        raise AssertionError("Expected invalid collect timeout to be rejected")


def test_schedule_run_task_tracks_and_removes_completed_task(monkeypatch):
    async def run_task_lifecycle():
        main._RUN_TASKS.clear()

        async def fake_execute_run(run_id):
            return None

        monkeypatch.setattr(main, "execute_run", fake_execute_run)
        task = main._schedule_run_task("run123")

        assert main._RUN_TASKS["run123"] is task
        await task
        await asyncio.sleep(0)
        assert "run123" not in main._RUN_TASKS

    try:
        asyncio.run(run_task_lifecycle())
    finally:
        main._RUN_TASKS.clear()


def test_shutdown_cancels_active_run_tasks(monkeypatch):
    cancelled = []

    async def run_shutdown():
        main._RUN_TASKS.clear()
        started = asyncio.Event()

        async def fake_execute_run(run_id):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(run_id)
                raise

        monkeypatch.setattr(main, "execute_run", fake_execute_run)
        main._schedule_run_task("run456")
        await started.wait()
        await main.cancel_active_run_tasks_on_shutdown()

    try:
        asyncio.run(run_shutdown())
    finally:
        main._RUN_TASKS.clear()

    assert cancelled == ["run456"]
    assert main._RUN_TASKS == {}
