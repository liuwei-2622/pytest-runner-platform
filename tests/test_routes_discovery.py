from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.projects import ProjectConfig


def make_project(root: Path) -> ProjectConfig:
    return ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(root),
        python_executable="python3",
        working_directory=str(root),
        allowed_test_roots=[str(root)],
    )


class FakeIndexCache:
    def __init__(self):
        self.calls = []

    async def get_suggestions(self, project, query, limit=50):
        self.calls.append((project.id, query, limit))
        return [{"value": "tests/test_api.py", "label": "tests/test_api.py", "kind": "file"}]


def test_test_targets_route_uses_index_cache_and_preserves_schema(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    cache = FakeIndexCache()

    monkeypatch.setattr(main, "get_project", lambda project_id: project if project_id == "demo" else None)
    monkeypatch.setattr(main, "test_target_index_cache", cache)

    response = TestClient(main.app).get("/api/projects/demo/test-targets?q=api")

    assert response.status_code == 200
    assert response.json() == {
        "project_id": "demo",
        "suggestions": [{"value": "tests/test_api.py", "label": "tests/test_api.py", "kind": "file"}],
    }
    assert cache.calls == [("demo", "api", 50)]


def test_test_targets_route_truncates_query_for_cache(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    cache = FakeIndexCache()

    monkeypatch.setattr(main, "get_project", lambda project_id: project if project_id == "demo" else None)
    monkeypatch.setattr(main, "test_target_index_cache", cache)

    response = TestClient(main.app).get(f"/api/projects/demo/test-targets?q={'a' * 600}")

    assert response.status_code == 200
    assert len(cache.calls[0][1]) == 512


def test_test_targets_route_still_returns_404_for_missing_project(monkeypatch):
    monkeypatch.setattr(main, "get_project", lambda project_id: None)

    response = TestClient(main.app).get("/api/projects/missing/test-targets?q=api")

    assert response.status_code == 404
