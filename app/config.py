import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = BASE_DIR / "tests_workspace"
REPORTS_DIR = BASE_DIR / "reports"
RUN_METADATA_DB = Path(os.getenv("PYTEST_PLATFORM_RUN_METADATA_DB", str(REPORTS_DIR / "runs.sqlite3")))


def default_max_workers(cpu_count: int | None = None) -> int:
    effective_cpu_count = cpu_count or 2
    return max(1, min(8, effective_cpu_count // 2))


MAX_CONCURRENT_RUNS = int(os.getenv("PYTEST_PLATFORM_MAX_CONCURRENT_RUNS", "2"))
RUN_TIMEOUT_SECONDS = int(os.getenv("PYTEST_PLATFORM_RUN_TIMEOUT_SECONDS", "1800"))
MAX_LOG_PREVIEW_BYTES = int(os.getenv("PYTEST_PLATFORM_MAX_LOG_PREVIEW_BYTES", "120000"))
COLLECT_TIMEOUT_SECONDS = int(os.getenv("PYTEST_PLATFORM_COLLECT_TIMEOUT_SECONDS", "20"))
MAX_COLLECT_OUTPUT_BYTES = int(os.getenv("PYTEST_PLATFORM_MAX_COLLECT_OUTPUT_BYTES", "120000"))
MAX_WORKERS = int(os.getenv("PYTEST_PLATFORM_MAX_WORKERS", str(default_max_workers(os.cpu_count()))))
REPORT_PLUGIN_MODE = os.getenv("PYTEST_PLATFORM_REPORT_PLUGIN_MODE", "auto").strip().lower()
if REPORT_PLUGIN_MODE not in {"auto", "strict", "builtin"}:
    REPORT_PLUGIN_MODE = "auto"
ALLOWED_TB_VALUES = {"auto", "long", "short", "line", "native", "no"}
