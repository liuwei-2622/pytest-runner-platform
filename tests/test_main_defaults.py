from pathlib import Path

from app.main import _project_default_test_target
from app.projects import ProjectConfig


def make_project(root: Path, allowed_roots: list[Path]) -> ProjectConfig:
    return ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(root),
        python_executable="python3",
        working_directory=str(root),
        allowed_test_roots=[str(path) for path in allowed_roots],
    )


def test_project_default_test_target_uses_first_allowed_root_relative_to_project_root(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)

    assert _project_default_test_target(make_project(root, [tests])) == "tests"


def test_project_default_test_target_returns_dot_when_project_root_is_allowed(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    assert _project_default_test_target(make_project(root, [root])) == "."
