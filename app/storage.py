from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from .config import (
    MAX_LOG_PREVIEW_BYTES,
    REPORTS_DIR,
    RUN_METADATA_DB,
    RUN_RETENTION_MAX_AGE_DAYS,
    RUN_RETENTION_MAX_COUNT,
)
from .history import COMPLETED_STATUSES, FAILURE_STATUSES, HistorySummary, TrendPoint
from .models import RunOptions, RunProgress, TestRun, utc_now

_lock = Lock()
ACTIVE_STATUSES = {"queued", "running"}
_initialized_storage: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class DeleteRunsResult:
    deleted: int = 0
    skipped_active: int = 0
    missing: int = 0
    skipped_invalid_report_dir: int = 0
    artifact_delete_failed: int = 0


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
"""

INDEX_SCHEMA = """
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


RUN_COLUMN_MIGRATIONS = {
    "status": "TEXT NOT NULL DEFAULT 'queued'",
    "created_at": "TEXT NOT NULL DEFAULT ''",
    "started_at": "TEXT",
    "finished_at": "TEXT",
    "project_id": "TEXT NOT NULL DEFAULT 'sample'",
    "project_name": "TEXT NOT NULL DEFAULT 'Sample Workspace'",
    "test_path": "TEXT NOT NULL DEFAULT ''",
    "resolved_test_path": "TEXT NOT NULL DEFAULT ''",
    "return_code": "INTEGER",
    "report_dir": "TEXT NOT NULL DEFAULT ''",
    "html_report_path": "TEXT NOT NULL DEFAULT ''",
    "junit_report_path": "TEXT NOT NULL DEFAULT ''",
    "stdout_path": "TEXT NOT NULL DEFAULT ''",
    "stderr_path": "TEXT NOT NULL DEFAULT ''",
    "allure_results_path": "TEXT NOT NULL DEFAULT ''",
    "allure_report_path": "TEXT NOT NULL DEFAULT ''",
    "error_message": "TEXT NOT NULL DEFAULT ''",
    "command_json": "TEXT NOT NULL DEFAULT '[]'",
    "options_json": "TEXT NOT NULL DEFAULT '{}'",
    "progress_json": "TEXT NOT NULL DEFAULT '{}'",
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
}


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_run_columns(conn)
    _backfill_row_metadata(conn)
    conn.executescript(INDEX_SCHEMA)


def _migrate_run_columns(conn: sqlite3.Connection) -> None:
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for column, definition in RUN_COLUMN_MIGRATIONS.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")


def _json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _row_value(row: sqlite3.Row, key: str, default=""):
    value = row[key]
    return default if value is None else value


def _row_metadata(row: sqlite3.Row) -> dict:
    run_id = _row_value(row, "id")
    report_dir = _row_value(row, "report_dir") or str(REPORTS_DIR / run_id)
    return TestRun.from_dict(
        {
            "id": run_id,
            "status": _row_value(row, "status", "queued"),
            "created_at": _row_value(row, "created_at", utc_now()),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "test_path": _row_value(row, "test_path"),
            "resolved_test_path": _row_value(row, "resolved_test_path"),
            "options": _json_loads(row["options_json"], {}),
            "return_code": row["return_code"],
            "command": _json_loads(row["command_json"], []),
            "report_dir": report_dir,
            "html_report_path": _row_value(row, "html_report_path") or str(Path(report_dir) / "pytest.html"),
            "junit_report_path": _row_value(row, "junit_report_path") or str(Path(report_dir) / "junit.xml"),
            "stdout_path": _row_value(row, "stdout_path") or str(Path(report_dir) / "stdout.log"),
            "stderr_path": _row_value(row, "stderr_path") or str(Path(report_dir) / "stderr.log"),
            "allure_results_path": _row_value(row, "allure_results_path") or str(Path(report_dir) / "allure-results"),
            "allure_report_path": _row_value(row, "allure_report_path") or str(Path(report_dir) / "allure-report"),
            "project_id": _row_value(row, "project_id", "sample"),
            "project_name": _row_value(row, "project_name", "Sample Workspace"),
            "error_message": _row_value(row, "error_message"),
            "progress": _json_loads(row["progress_json"], {}),
        }
    ).to_dict()


def _backfill_row_metadata(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM runs WHERE metadata_json IS NULL OR metadata_json IN ('', '{}')").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE runs SET metadata_json = ?, command_json = ?, options_json = ?, progress_json = ? WHERE id = ?",
            (
                _json_dumps(_row_metadata(row)),
                _json_dumps(_json_loads(row["command_json"], [])),
                _json_dumps(_json_loads(row["options_json"], {})),
                _json_dumps(_json_loads(row["progress_json"], {})),
                row["id"],
            ),
        )


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
    query = "SELECT metadata_json FROM runs ORDER BY created_at DESC, id DESC"
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


def _rows_to_runs(rows: list[sqlite3.Row]) -> list[TestRun]:
    runs: list[TestRun] = []
    for row in rows:
        try:
            runs.append(_row_to_run(row))
        except (json.JSONDecodeError, TypeError):
            continue
    return runs


def _trend_point(run: TestRun) -> TrendPoint:
    return TrendPoint(
        run_id=run.id,
        created_at=run.created_at,
        status=run.status,
        duration_seconds=run.duration_seconds,
        total=run.progress.collected or run.progress.completed or 0,
        failed=run.progress.failed,
        errors=run.progress.errors,
        skipped=run.progress.skipped,
    )


def get_history_summary(limit: int = 30) -> HistorySummary:
    ensure_storage()
    completed_statuses = tuple(sorted(COMPLETED_STATUSES))
    failure_statuses = tuple(sorted(FAILURE_STATUSES))
    completed_placeholders = ", ".join("?" for _ in completed_statuses)
    failure_placeholders = ", ".join("?" for _ in failure_statuses)
    trend_limit = max(limit, 0)

    with _connect() as conn:
        total_runs = int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        completed_runs = int(
            conn.execute(
                f"SELECT COUNT(*) FROM runs WHERE status IN ({completed_placeholders})",
                completed_statuses,
            ).fetchone()[0]
        )
        passed_runs = int(conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'passed'").fetchone()[0])
        average_duration = conn.execute(
            f"""
            SELECT AVG((julianday(finished_at) - julianday(started_at)) * 86400.0)
            FROM runs
            WHERE status IN ({completed_placeholders})
              AND started_at IS NOT NULL
              AND finished_at IS NOT NULL
              AND started_at != ''
              AND finished_at != ''
            """,
            completed_statuses,
        ).fetchone()[0]
        status_counts = {
            row["status"]: int(row["count"])
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM runs GROUP BY status").fetchall()
        }
        recent_failure_rows = conn.execute(
            f"""
            SELECT metadata_json FROM runs
            WHERE status IN ({failure_placeholders})
            ORDER BY created_at DESC, id DESC
            LIMIT 5
            """,
            failure_statuses,
        ).fetchall()
        trend_rows = conn.execute(
            """
            SELECT metadata_json FROM runs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (trend_limit,),
        ).fetchall()

    pass_rate = round(passed_runs / completed_runs * 100, 1) if completed_runs else None
    average_duration_seconds = round(float(average_duration), 2) if average_duration is not None else None
    recent_failures = _rows_to_runs(recent_failure_rows)
    trend_points = [_trend_point(run) for run in reversed(_rows_to_runs(trend_rows))]
    return HistorySummary(
        total_runs=total_runs,
        completed_runs=completed_runs,
        pass_rate=pass_rate,
        average_duration_seconds=average_duration_seconds,
        status_counts=status_counts,
        recent_failures=recent_failures,
        trend_points=trend_points,
    )


def _safe_retention_value(value: int | None) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _retention_cutoff(max_age_days: int) -> str:
    now = datetime.fromisoformat(utc_now())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - timedelta(days=max_age_days)).isoformat()


def _add_unique_run_ids(target: list[str], run_ids: list[str]) -> None:
    seen = set(target)
    for run_id in run_ids:
        if run_id not in seen:
            target.append(run_id)
            seen.add(run_id)


def select_retention_run_ids(max_count: int | None = None, max_age_days: int | None = None) -> list[str]:
    ensure_storage()
    max_count = _safe_retention_value(RUN_RETENTION_MAX_COUNT if max_count is None else max_count)
    max_age_days = _safe_retention_value(RUN_RETENTION_MAX_AGE_DAYS if max_age_days is None else max_age_days)
    if not max_count and not max_age_days:
        return []

    active_statuses = tuple(sorted(ACTIVE_STATUSES))
    active_placeholders = ", ".join("?" for _ in active_statuses)
    candidates: list[str] = []
    with _connect() as conn:
        if max_count:
            rows = conn.execute(
                f"""
                SELECT id FROM (
                  SELECT id, status FROM runs ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?
                ) WHERE status NOT IN ({active_placeholders})
                """,
                (max_count, *active_statuses),
            ).fetchall()
            _add_unique_run_ids(candidates, [row["id"] for row in rows])

        if max_age_days:
            cutoff = _retention_cutoff(max_age_days)
            rows = conn.execute(
                f"""
                SELECT id FROM runs
                WHERE created_at != ''
                  AND created_at < ?
                  AND status NOT IN ({active_placeholders})
                ORDER BY created_at DESC, id DESC
                """,
                (cutoff, *active_statuses),
            ).fetchall()
            _add_unique_run_ids(candidates, [row["id"] for row in rows])
    return candidates


def cleanup_runs_by_retention(max_count: int | None = None, max_age_days: int | None = None) -> DeleteRunsResult:
    run_ids = select_retention_run_ids(max_count=max_count, max_age_days=max_age_days)
    if not run_ids:
        return DeleteRunsResult()
    return delete_runs(run_ids)


def _safe_report_dir_for_delete(run_id: str, report_dir: str) -> Path:
    reports_root = REPORTS_DIR.resolve()
    target = Path(report_dir).resolve()
    expected = (reports_root / run_id).resolve()
    if target != expected or target.parent != reports_root:
        raise ValueError(f"Refusing to delete invalid report directory for run {run_id}: {target}")
    return target


def format_delete_runs_message(result: DeleteRunsResult) -> str:
    parts: list[str] = []
    if result.deleted:
        parts.append(f"已删除 {result.deleted} 条运行记录")
    else:
        parts.append("未删除任何记录")
    if result.skipped_active:
        parts.append(f"跳过 {result.skipped_active} 条运行中记录")
    if result.missing:
        parts.append(f"忽略 {result.missing} 条不存在记录")
    if result.skipped_invalid_report_dir:
        parts.append(f"跳过 {result.skipped_invalid_report_dir} 条报告目录异常记录")
    if result.artifact_delete_failed:
        parts.append(f"{result.artifact_delete_failed} 条报告目录清理失败")
    return "，".join(parts) + "。"


def _row_to_delete_candidate(conn: sqlite3.Connection, run_id: str) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT status, report_dir FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None, None
    return row["status"], row["report_dir"]


def delete_runs(run_ids: list[str]) -> DeleteRunsResult:
    ensure_storage()
    deleted = 0
    skipped_active = 0
    missing = 0
    skipped_invalid_report_dir = 0
    artifact_delete_failed = 0
    unique_run_ids = list(dict.fromkeys(run_ids))

    for run_id in unique_run_ids:
        report_dir: Path | None = None
        with _lock, _connect() as conn:
            status, raw_report_dir = _row_to_delete_candidate(conn, run_id)
            if status is None:
                missing += 1
                continue
            if status in ACTIVE_STATUSES:
                skipped_active += 1
                continue
            try:
                report_dir = _safe_report_dir_for_delete(run_id, raw_report_dir or "")
            except ValueError:
                skipped_invalid_report_dir += 1
                continue
            cursor = conn.execute(
                "DELETE FROM runs WHERE id = ? AND status NOT IN (?, ?)",
                (run_id, *sorted(ACTIVE_STATUSES)),
            )
            if cursor.rowcount != 1:
                status, _raw_report_dir = _row_to_delete_candidate(conn, run_id)
                if status is None:
                    missing += 1
                elif status in ACTIVE_STATUSES:
                    skipped_active += 1
                else:
                    missing += 1
                continue

        try:
            shutil.rmtree(report_dir)
        except FileNotFoundError:
            pass
        except OSError:
            artifact_delete_failed += 1
        deleted += 1

    return DeleteRunsResult(
        deleted=deleted,
        skipped_active=skipped_active,
        missing=missing,
        skipped_invalid_report_dir=skipped_invalid_report_dir,
        artifact_delete_failed=artifact_delete_failed,
    )


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
