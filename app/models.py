from __future__ import annotations

from dataclasses import asdict, dataclass, field
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

    @classmethod
    def from_dict(cls, data: dict) -> "RunOptions":
        return cls(
            keyword=data.get("keyword", ""),
            marker=data.get("marker", ""),
            verbosity=data.get("verbosity", "normal"),
            maxfail=data.get("maxfail"),
            workers=data.get("workers", "disabled"),
            env_vars=data.get("env_vars", {}),
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

    def to_dict(self) -> dict:
        data = asdict(self)
        data["options"] = asdict(self.options)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TestRun":
        data = dict(data)
        data["options"] = RunOptions.from_dict(data.get("options", {}))
        data.setdefault("allure_results_path", "")
        data.setdefault("allure_report_path", "")
        data.setdefault("project_id", "sample")
        data.setdefault("project_name", "Sample Workspace")
        data.setdefault("error_message", "")
        return cls(**data)

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.finished_at:
            return None
        started = datetime.fromisoformat(self.started_at)
        finished = datetime.fromisoformat(self.finished_at)
        return round((finished - started).total_seconds(), 2)
