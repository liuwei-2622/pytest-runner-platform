import sys
from pathlib import Path

from app.config import COLLECT_TIMEOUT_SECONDS
from app.main import _project_default_test_target
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


def test_project_default_test_target_uses_first_allowed_root_relative_to_project_root(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)

    assert _project_default_test_target(make_project(root, [tests])) == "tests"


def test_project_default_test_target_returns_dot_when_project_root_is_allowed(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    assert _project_default_test_target(make_project(root, [root])) == "."


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
