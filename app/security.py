from __future__ import annotations

import re
from pathlib import Path

from .config import ALLOWED_WORKER_VALUES
from .models import RunOptions
from .projects import ProjectConfig

ALLOWED_VERBOSITY = {"normal", "quiet", "verbose"}
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV_INPUT_BYTES = 8192
MAX_ENV_VARS = 100


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_test_path(project: ProjectConfig, raw_path: str) -> tuple[str, str]:
    value = (raw_path or ".").strip() or "."
    path_value, separator, node_selector = value.partition("::")
    raw = Path(path_value or ".")
    candidate = raw if raw.is_absolute() else project.root / raw
    resolved = candidate.resolve()

    allowed_roots = project.allowed_roots
    if not any(_is_relative_to(resolved, allowed_root) for allowed_root in allowed_roots):
        raise ValueError("测试路径必须位于当前项目允许的测试目录内")
    if not resolved.exists():
        raise ValueError("测试路径不存在")

    display_path = "." if resolved == project.root else str(resolved.relative_to(project.root))
    resolved_target = str(resolved)
    if separator:
        display_path = f"{display_path}::{node_selector}"
        resolved_target = f"{resolved_target}::{node_selector}"
    return display_path, resolved_target


def validate_env_vars(raw: str) -> dict[str, str]:
    if len(raw.encode("utf-8")) > MAX_ENV_INPUT_BYTES:
        raise ValueError("环境变量配置不能超过 8 KiB")

    env_vars: dict[str, str] = {}
    for line_number, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"第 {line_number} 行环境变量缺少 =")

        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_PATTERN.match(key):
            raise ValueError(f"第 {line_number} 行环境变量名称无效: {key}")
        if any(ord(char) < 32 and char not in "\t" for char in value):
            raise ValueError(f"第 {line_number} 行环境变量值包含非法控制字符")

        env_vars[key] = value
        if len(env_vars) > MAX_ENV_VARS:
            raise ValueError("环境变量数量不能超过 100 个")

    return env_vars


def validate_options(
    keyword: str,
    marker: str,
    verbosity: str,
    maxfail: str,
    workers: str,
) -> RunOptions:
    verbosity = verbosity if verbosity in ALLOWED_VERBOSITY else "normal"
    workers = workers if workers in ALLOWED_WORKER_VALUES else "disabled"
    parsed_maxfail = None

    if maxfail.strip():
        try:
            parsed_maxfail = int(maxfail)
        except ValueError as exc:
            raise ValueError("maxfail 必须是正整数") from exc
        if parsed_maxfail < 1 or parsed_maxfail > 1000:
            raise ValueError("maxfail 必须在 1 到 1000 之间")

    return RunOptions(
        keyword=keyword.strip(),
        marker=marker.strip(),
        verbosity=verbosity,
        maxfail=parsed_maxfail,
        workers=workers,
    )
