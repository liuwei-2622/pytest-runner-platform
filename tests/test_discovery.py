from pathlib import Path

from app.discovery import build_test_target_index, filter_test_target_suggestions, list_test_target_suggestions
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


def test_discovery_prunes_ignored_directories(tmp_path):
    root = tmp_path / "project"
    ignored_tests = root / ".venv" / "tests"
    ignored_tests.mkdir(parents=True)
    (ignored_tests / "test_hidden.py").write_text(
        "def test_should_not_be_seen():\n    pass\n",
        encoding="utf-8",
    )

    visible_tests = root / "tests"
    visible_tests.mkdir()
    (visible_tests / "test_visible.py").write_text(
        "def test_visible():\n    pass\n",
        encoding="utf-8",
    )

    values = {
        suggestion["value"]
        for suggestion in list_test_target_suggestions(make_project(root), limit=50)
    }

    assert "tests/test_visible.py" in values
    assert "tests/test_visible.py::test_visible" in values
    assert not any(".venv" in value for value in values)
    assert not any("test_should_not_be_seen" in value for value in values)


def test_discovery_includes_async_test_functions(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_async.py").write_text(
        """
async def test_top_level_async():
    pass

class TestAsyncCases:
    async def test_method_async(self):
        pass
""",
        encoding="utf-8",
    )

    values = {
        suggestion["value"]
        for suggestion in list_test_target_suggestions(make_project(root), limit=50)
    }

    assert "tests/test_async.py::test_top_level_async" in values
    assert "tests/test_async.py::TestAsyncCases::test_method_async" in values


def test_build_test_target_index_returns_unfiltered_suggestions(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_api.py").write_text("def test_login():\n    pass\n", encoding="utf-8")
    (tests / "test_web.py").write_text("def test_home():\n    pass\n", encoding="utf-8")

    values = {suggestion["value"] for suggestion in build_test_target_index(make_project(root))}

    assert "tests" in values
    assert "tests/test_api.py" in values
    assert "tests/test_api.py::test_login" in values
    assert "tests/test_web.py::test_home" in values


def test_filter_test_target_suggestions_filters_cached_index_without_rescan():
    suggestions = [
        {"value": "tests/test_api.py", "label": "tests/test_api.py", "kind": "file"},
        {"value": "tests/test_web.py", "label": "tests/test_web.py", "kind": "file"},
        {"value": "tests/test_api.py::test_login", "label": "test_login", "kind": "test"},
    ]

    filtered = filter_test_target_suggestions(suggestions, "login", limit=10)

    assert filtered == [
        {"value": "tests/test_api.py::test_login", "label": "test_login", "kind": "test"},
    ]


def test_filter_test_target_suggestions_applies_limit():
    suggestions = [
        {"value": f"tests/test_{index}.py", "label": f"tests/test_{index}.py", "kind": "file"}
        for index in range(3)
    ]

    assert len(filter_test_target_suggestions(suggestions, "", limit=2)) == 2
