from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from threading import Lock

from .config import MAX_LOG_PREVIEW_BYTES, REPORTS_DIR, RUN_METADATA_DB
from .models import RunOptions, RunProgress, TestRun, utc_now

_lock = Lock()
ACTIVE_STATUSES = {"queued", "running"}
_initialized_storage: set[tuple[str, str]] = set()


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  project_id TEXT NOT NULL,
  project_name TEXT NOT NULL,
  test_path TEXT NOT NULL,
  resolved_test_path TEXT NOT NULL,
  return_code INTEGER,
  report_dir TEXT NOT NULL,
  html_report_path TEXT NOT NULL,
  junit_report_path TEXT NOT NULL,
  stdout_path TEXT NOT NULL,
  stderr_path TEXT NOT NULL,
  allure_results_path TEXT NOT NULL DEFAULT '',
  allure_report_path TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  command_json TEXT NOT NULL DEFAULT '[]',
  options_json TEXT NOT NULL DEFAULT '{}',
  progress_json TEXT NOT NULL DEFAULT '{}',
  metadata_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
"""


def ensure_storage() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    key = (str(RUN_METADATA_DB.resolve()), str(REPORTS_DIR.resolve()))
    if key in _initialized_storage:
        return
    with _connect() as conn:
        _init_db(conn)
        _backfill_legacy_metadata(conn)
    _initialized_storage.add(key)


def _connect() -> sqlite3.Connection:
    RUN_METADATA_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(RUN_METADATA_DB)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _run_to_row(run: TestRun) -> dict:
    metadata = run.to_dict()
    return {
        "id": run.id,
        "status": run.status,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "project_id": run.project_id,
        "project_name": run.project_name,
        "test_path": run.test_path,
        "resolved_test_path": run.resolved_test_path,
        "return_code": run.return_code,
        "report_dir": run.report_dir,
        "html_report_path": run.html_report_path,
        "junit_report_path": run.junit_report_path,
        "stdout_path": run.stdout_path,
        "stderr_path": run.stderr_path,
        "allure_results_path": run.allure_results_path,
        "allure_report_path": run.allure_report_path,
        "error_message": run.error_message,
        "command_json": _json_dumps(run.command),
        "options_json": _json_dumps(metadata["options"]),
        "progress_json": _json_dumps(metadata["progress"]),
        "metadata_json": _json_dumps(metadata),
        "updated_at": utc_now(),
    }


def _row_to_run(row: sqlite3.Row) -> TestRun:
    return TestRun.from_dict(json.loads(row["metadata_json"]))


def _insert_run_ignore(conn: sqlite3.Connection, run: TestRun) -> None:
    row = _run_to_row(run)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    conn.execute(f"INSERT OR IGNORE INTO runs ({columns}) VALUES ({placeholders})", row)


def _upsert_run(conn: sqlite3.Connection, run: TestRun) -> None:
    row = _run_to_row(run)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "id")
    conn.execute(
        f"INSERT INTO runs ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
        row,
    )


def _backfill_legacy_metadata(conn: sqlite3.Connection) -> int:
    imported = 0
    for path in REPORTS_DIR.glob("*/metadata.json"):
        try:
            run = TestRun.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        before = conn.total_changes
        _insert_run_ignore(conn, run)
        if conn.total_changes > before:
            imported += 1
    return imported


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
    with _lock, _connect() as conn:
        _upsert_run(conn, run)
    return run


def get_run(run_id: str) -> TestRun | None:
    ensure_storage()
    with _connect() as conn:
        row = conn.execute("SELECT metadata_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    try:
        return _row_to_run(row)
    except (json.JSONDecodeError, TypeError):
        return None


def update_run(run_id: str, **changes) -> TestRun:
    with _lock:
        run = get_run(run_id)
        if not run:
            raise KeyError(run_id)
        for key, value in changes.items():
            setattr(run, key, value)
        with _connect() as conn:
            _upsert_run(conn, run)
        return run


def update_run_progress(run_id: str, progress: RunProgress) -> TestRun:
    return update_run(run_id, progress=progress)


def list_runs(limit: int | None = None, offset: int = 0) -> list[TestRun]:
    ensure_storage()
    runs: list[TestRun] = []
    query = "SELECT metadata_json FROM runs ORDER BY created_at DESC"
    parameters: tuple[int, ...] = ()
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        parameters = (limit, max(offset, 0))
    with _connect() as conn:
        rows = conn.execute(query, parameters).fetchall()
    for row in rows:
        try:
            runs.append(_row_to_run(row))
        except (json.JSONDecodeError, TypeError):
            continue
    return runs


def count_runs() -> int:
    ensure_storage()
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])


def recover_stale_runs(reason: str = "应用重启后恢复中断的运行") -> int:
    recovered = 0
    for run in list_runs():
        if run.status not in ACTIVE_STATUSES:
            continue
        try:
            update_run(run.id, status="error", finished_at=utc_now(), error_message=reason)
        except KeyError:
            continue
        recovered += 1
    return recovered


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
