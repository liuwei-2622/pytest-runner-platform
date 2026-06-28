from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

from .config import BASE_DIR, COLLECT_TIMEOUT_SECONDS

PROJECTS_PATH = BASE_DIR / "projects.json"
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_lock = Lock()


@dataclass
class ProjectConfig:
    id: str
    name: str
    root_path: str
    python_executable: str
    working_directory: str
    allowed_test_roots: list[str] = field(default_factory=list)
    default_args: list[str] = field(default_factory=list)
    default_env: dict[str, str] = field(default_factory=dict)
    report_mode: str = "platform"
    collect_timeout_seconds: int = COLLECT_TIMEOUT_SECONDS

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            root_path=data.get("root_path", ""),
            python_executable=data.get("python_executable", ""),
            working_directory=data.get("working_directory", ""),
            allowed_test_roots=list(data.get("allowed_test_roots", [])),
            default_args=list(data.get("default_args", [])),
            default_env=dict(data.get("default_env", {})),
            report_mode=data.get("report_mode", "platform"),
            collect_timeout_seconds=int(data.get("collect_timeout_seconds", COLLECT_TIMEOUT_SECONDS)),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def root(self) -> Path:
        return Path(self.root_path).expanduser().resolve()

    @property
    def working_dir(self) -> Path:
        return Path(self.working_directory).expanduser().resolve()

    @property
    def python_path(self) -> Path:
        return Path(self.python_executable).expanduser().resolve()

    @property
    def allowed_roots(self) -> list[Path]:
        return [Path(path).expanduser().resolve() for path in self.allowed_test_roots]


def default_project() -> ProjectConfig:
    demo_project = BASE_DIR / "demo_project"
    return ProjectConfig(
        id="demo",
        name="Demo Project",
        root_path=str(demo_project),
        python_executable=sys.executable,
        working_directory=str(demo_project),
        allowed_test_roots=[str(demo_project / "tests")],
        default_args=[],
        default_env={"DEMO_PROJECT_ENV": "ok"},
        report_mode="platform",
        collect_timeout_seconds=COLLECT_TIMEOUT_SECONDS,
    )


def _read_projects_file() -> list[ProjectConfig]:
    if not PROJECTS_PATH.exists():
        return [default_project()]
    data = json.loads(PROJECTS_PATH.read_text(encoding="utf-8"))
    return [ProjectConfig.from_dict(item) for item in data.get("projects", [])]


def load_projects() -> list[ProjectConfig]:
    projects = _read_projects_file()
    if not projects:
        return [default_project()]
    return sorted(projects, key=lambda project: project.name.lower())


def save_projects(projects: list[ProjectConfig]) -> None:
    payload = {"projects": [project.to_dict() for project in projects]}
    tmp_path = PROJECTS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(PROJECTS_PATH)


def list_projects() -> list[ProjectConfig]:
    return load_projects()


def get_project(project_id: str) -> ProjectConfig | None:
    for project in load_projects():
        if project.id == project_id:
            return project
    return None


def default_project_id() -> str:
    return load_projects()[0].id


def validate_project(project: ProjectConfig) -> ProjectConfig:
    project.id = project.id.strip()
    project.name = project.name.strip()
    project.root_path = str(Path(project.root_path).expanduser().resolve())
    project.working_directory = str(Path(project.working_directory).expanduser().resolve())
    project.python_executable = str(Path(project.python_executable).expanduser().resolve())
    project.allowed_test_roots = [
        str(Path(path).expanduser().resolve())
        for path in project.allowed_test_roots
        if path.strip()
    ]
    project.report_mode = project.report_mode or "platform"
    project.collect_timeout_seconds = int(project.collect_timeout_seconds)

    if not PROJECT_ID_PATTERN.match(project.id):
        raise ValueError("项目 ID 只能包含字母、数字、下划线和中划线")
    if not project.name:
        raise ValueError("项目名称不能为空")
    if not Path(project.root_path).is_dir():
        raise ValueError("项目根目录不存在")
    if not Path(project.working_directory).is_dir():
        raise ValueError("工作目录不存在")
    if not Path(project.python_executable).is_file():
        raise ValueError("Python 解释器不存在")
    if project.report_mode != "platform":
        raise ValueError("当前仅支持 platform 报告模式")
    if project.collect_timeout_seconds < 5 or project.collect_timeout_seconds > 3600:
        raise ValueError("收集超时秒数必须在 5 到 3600 之间")
    if not project.allowed_test_roots:
        raise ValueError("至少需要一个允许测试目录")

    root = Path(project.root_path).resolve()
    for allowed_root in project.allowed_roots:
        if not allowed_root.exists():
            raise ValueError(f"允许测试目录不存在: {allowed_root}")
        if allowed_root != root and root not in allowed_root.parents:
            raise ValueError(f"允许测试目录必须位于项目根目录内: {allowed_root}")

    return project


def upsert_project(project: ProjectConfig) -> None:
    project = validate_project(project)
    with _lock:
        projects = load_projects()
        existing_ids = {item.id for item in projects}
        if project.id in existing_ids:
            projects = [project if item.id == project.id else item for item in projects]
        else:
            projects.append(project)
        save_projects(projects)


def delete_project(project_id: str) -> None:
    with _lock:
        projects = load_projects()
        if len(projects) <= 1:
            raise ValueError("不能删除最后一个项目")
        remaining = [project for project in projects if project.id != project_id]
        if len(remaining) == len(projects):
            raise ValueError("项目不存在")
        save_projects(remaining)
