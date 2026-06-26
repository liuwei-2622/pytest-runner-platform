from __future__ import annotations

import json
import uuid
from pathlib import Path
from threading import Lock

from .config import MAX_LOG_PREVIEW_BYTES, REPORTS_DIR
from .models import RunOptions, TestRun, utc_now

_lock = Lock()


def ensure_storage() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _metadata_path(run_id: str) -> Path:
    return REPORTS_DIR / run_id / "metadata.json"


def _write_run(run: TestRun) -> None:
    path = _metadata_path(run.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def create_run(
    project_id: str,
    project_name: str,
    test_path: str,
    resolved_test_path: Path,
    options: RunOptions,
) -> TestRun:
    ensure_storage()
    run_id = uuid.uuid4().hex[:12]
    report_dir = REPORTS_DIR / run_id
    report_dir.mkdir(parents=True, exist_ok=False)

    run = TestRun(
        id=run_id,
        status="queued",
        created_at=utc_now(),
        started_at=None,
        finished_at=None,
        test_path=test_path,
        resolved_test_path=str(resolved_test_path),
        options=options,
        return_code=None,
        command=[],
        report_dir=str(report_dir),
        html_report_path=str(report_dir / "pytest.html"),
        junit_report_path=str(report_dir / "junit.xml"),
        stdout_path=str(report_dir / "stdout.log"),
        stderr_path=str(report_dir / "stderr.log"),
        allure_results_path=str(report_dir / "allure-results"),
        allure_report_path=str(report_dir / "allure-report"),
        project_id=project_id,
        project_name=project_name,
    )
    with _lock:
        _write_run(run)
    return run


def get_run(run_id: str) -> TestRun | None:
    path = _metadata_path(run_id)
    if not path.exists():
        return None
    return TestRun.from_dict(json.loads(path.read_text(encoding="utf-8")))


def update_run(run_id: str, **changes) -> TestRun:
    with _lock:
        run = get_run(run_id)
        if not run:
            raise KeyError(run_id)
        for key, value in changes.items():
            setattr(run, key, value)
        _write_run(run)
        return run


def list_runs() -> list[TestRun]:
    ensure_storage()
    runs: list[TestRun] = []
    for path in REPORTS_DIR.glob("*/metadata.json"):
        try:
            runs.append(TestRun.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return sorted(runs, key=lambda item: item.created_at, reverse=True)


def artifact_path(run_id: str, artifact: str) -> Path | None:
    run = get_run(run_id)
    if not run:
        return None
    mapping = {
        "pytest.html": Path(run.html_report_path),
        "junit.xml": Path(run.junit_report_path),
        "stdout.log": Path(run.stdout_path),
        "stderr.log": Path(run.stderr_path),
    }
    path = mapping.get(artifact)
    if not path or not path.exists():
        return None
    return path


def read_log_preview(path: str) -> str:
    log_path = Path(path)
    if not log_path.exists():
        return ""
    data = log_path.read_bytes()[-MAX_LOG_PREVIEW_BYTES:]
    return data.decode("utf-8", errors="replace")
