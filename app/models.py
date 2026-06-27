from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Literal

RunStatus = Literal["queued", "running", "passed", "failed", "error", "timeout"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunOptions:
    keyword: str = ""
    marker: str = ""
    verbosity: str = "normal"
    maxfail: int | None = None
    workers: str = "disabled"
    env_vars: dict[str, str] = field(default_factory=dict)
    last_failed: bool = False
    failed_first: bool = False
    tb: str = "auto"
    env_var_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "RunOptions":
        return cls(
            keyword=data.get("keyword", ""),
            marker=data.get("marker", ""),
            verbosity=data.get("verbosity", "normal"),
            maxfail=data.get("maxfail"),
            workers=data.get("workers", "disabled"),
            env_vars=data.get("env_vars", {}),
            last_failed=data.get("last_failed", False),
            failed_first=data.get("failed_first", False),
            tb=data.get("tb", "auto"),
            env_var_keys=list(data.get("env_var_keys", [])),
        )


@dataclass
class RunTemplate:
    id: str
    project_id: str
    name: str
    test_path: str
    options: RunOptions
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["options"] = asdict(self.options)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RunTemplate":
        return cls(
            id=data.get("id", ""),
            project_id=data.get("project_id", ""),
            name=data.get("name", ""),
            test_path=data.get("test_path", ""),
            options=RunOptions.from_dict(data.get("options", {})),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class RunProgress:
    collected: int | None = None
    completed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    xfailed: int = 0
    xpassed: int = 0
    percent: float | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> "RunProgress":
        data = data or {}
        return cls(
            collected=data.get("collected"),
            completed=data.get("completed", 0),
            passed=data.get("passed", 0),
            failed=data.get("failed", 0),
            skipped=data.get("skipped", 0),
            errors=data.get("errors", 0),
            xfailed=data.get("xfailed", 0),
            xpassed=data.get("xpassed", 0),
            percent=data.get("percent"),
            updated_at=data.get("updated_at"),
        )


@dataclass
class TestRun:
    id: str
    status: RunStatus
    created_at: str
    started_at: str | None
    finished_at: str | None
    test_path: str
    resolved_test_path: str
    options: RunOptions
    return_code: int | None
    command: list[str]
    report_dir: str
    html_report_path: str
    junit_report_path: str
    stdout_path: str
    stderr_path: str
    allure_results_path: str = ""
    allure_report_path: str = ""
    project_id: str = "sample"
    project_name: str = "Sample Workspace"
    error_message: str = ""
    progress: RunProgress = field(default_factory=RunProgress)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["options"] = asdict(self.options)
        data["progress"] = asdict(self.progress)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TestRun":
        data = dict(data)
        data["options"] = RunOptions.from_dict(data.get("options", {}))
        data["progress"] = RunProgress.from_dict(data.get("progress"))
        data.setdefault("allure_results_path", "")
        data.setdefault("allure_report_path", "")
        data.setdefault("project_id", "sample")
        data.setdefault("project_name", "Sample Workspace")
        data.setdefault("error_message", "")
        allowed_fields = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed_fields})

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.finished_at:
            return None
        started = datetime.fromisoformat(self.started_at)
        finished = datetime.fromisoformat(self.finished_at)
        return round((finished - started).total_seconds(), 2)
