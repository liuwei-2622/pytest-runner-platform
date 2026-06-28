import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.projects import ProjectConfig


def make_project(tmp_path: Path) -> ProjectConfig:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    return ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(tmp_path),
        python_executable=sys.executable,
        working_directory=str(tmp_path),
        allowed_test_roots=[str(tests_dir)],
        collect_timeout_seconds=37,
    )


def test_collect_stream_route_returns_ndjson_events(tmp_path, monkeypatch):
    project = make_project(tmp_path)

    async def fake_stream_collect_tests(project_arg, resolved_path, options):
        yield {
            "event": "start",
            "display_command": "python -m pytest tests --collect-only",
            "timeout_seconds": project_arg.collect_timeout_seconds,
        }
        yield {"event": "stdout", "text": "collected 1 item\n"}
        yield {"event": "complete", "ok": True, "return_code": 0, "collected_count": 1, "timed_out": False, "error": ""}

    monkeypatch.setattr(main, "get_project", lambda project_id: project if project_id == "demo" else None)
    monkeypatch.setattr(main, "stream_collect_tests", fake_stream_collect_tests)

    response = TestClient(main.app).post("/api/collect/stream", data={"project_id": "demo", "test_path": "tests"})

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["event"] for event in events] == ["start", "stdout", "complete"]
    assert events[0]["timeout_seconds"] == 37
    assert events[1]["text"] == "collected 1 item\n"
    assert events[2]["collected_count"] == 1


def test_collect_json_route_remains_compatible(tmp_path, monkeypatch):
    project = make_project(tmp_path)

    async def fake_collect_tests(project_arg, resolved_path, options):
        return {
            "ok": True,
            "return_code": 0,
            "command": ["python", "-m", "pytest"],
            "display_command": "python -m pytest",
            "collected_count": 1,
            "stdout": "collected 1 item\n",
            "stderr": "",
            "timed_out": False,
            "error": "",
            "timeout_seconds": project_arg.collect_timeout_seconds,
        }

    monkeypatch.setattr(main, "get_project", lambda project_id: project if project_id == "demo" else None)
    monkeypatch.setattr(main, "collect_tests", fake_collect_tests)

    response = TestClient(main.app).post("/api/collect", data={"project_id": "demo", "test_path": "tests"})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["collected_count"] == 1
    assert data["timeout_seconds"] == 37
