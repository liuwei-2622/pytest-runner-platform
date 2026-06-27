from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ALLOWED_TB_VALUES, MAX_WORKERS
from .models import RunOptions
from .projects import ProjectConfig

ALLOWED_VERBOSITY = {"normal", "quiet", "verbose"}
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV_INPUT_BYTES = 8192
MAX_ENV_VARS = 100


@dataclass(frozen=True)
class EnvVarLine:
    line: int
    raw: str
    key: str
    value: str
    state: Literal["empty", "valid", "error"]
    message: str = ""


@dataclass(frozen=True)
class EnvVarLineIssue:
    line: int
    message: str
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class EnvVarValidationResult:
    ok: bool
    env_vars: dict[str, str]
    lines: list[EnvVarLine]
    issues: list[EnvVarLineIssue]


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


def validate_env_vars_detailed(raw: str) -> EnvVarValidationResult:
    env_vars: dict[str, str] = {}
    lines: list[EnvVarLine] = []
    issues: list[EnvVarLineIssue] = []

    if len(raw.encode("utf-8")) > MAX_ENV_INPUT_BYTES:
        message = "环境变量配置不能超过 8 KiB"
        issues.append(EnvVarLineIssue(line=0, message=message))
        return EnvVarValidationResult(ok=False, env_vars={}, lines=[], issues=issues)

    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            lines.append(EnvVarLine(line=line_number, raw=raw_line, key="", value="", state="empty"))
            continue
        if "=" not in line:
            message = f"第 {line_number} 行环境变量缺少 ="
            lines.append(EnvVarLine(line=line_number, raw=raw_line, key="", value="", state="error", message=message))
            issues.append(EnvVarLineIssue(line=line_number, message=message))
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_PATTERN.match(key):
            message = f"第 {line_number} 行环境变量名称无效: {key}"
            lines.append(EnvVarLine(line=line_number, raw=raw_line, key=key, value=value, state="error", message=message))
            issues.append(EnvVarLineIssue(line=line_number, message=message))
            continue
        if any(ord(char) < 32 and char not in "\t" for char in value):
            message = f"第 {line_number} 行环境变量值包含非法控制字符"
            lines.append(EnvVarLine(line=line_number, raw=raw_line, key=key, value=value, state="error", message=message))
            issues.append(EnvVarLineIssue(line=line_number, message=message))
            continue

        env_vars[key] = value
        lines.append(EnvVarLine(line=line_number, raw=raw_line, key=key, value=value, state="valid"))
        if len(env_vars) > MAX_ENV_VARS:
            message = "环境变量数量不能超过 100 个"
            issues.append(EnvVarLineIssue(line=line_number, message=message))
            lines[-1] = EnvVarLine(line=line_number, raw=raw_line, key=key, value=value, state="error", message=message)

    return EnvVarValidationResult(ok=not issues, env_vars=env_vars if not issues else {}, lines=lines, issues=issues)


def validate_env_vars(raw: str) -> dict[str, str]:
    result = validate_env_vars_detailed(raw)
    if not result.ok:
        raise ValueError(result.issues[0].message)
    return result.env_vars


def _parse_bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return value.lower() in {"1", "true", "yes", "on"}


def validate_options(
    keyword: str,
    marker: str,
    verbosity: str,
    maxfail: str,
    workers: str,
    last_failed: bool | str = False,
    failed_first: bool | str = False,
    tb: str = "auto",
) -> RunOptions:
    verbosity = verbosity if verbosity in ALLOWED_VERBOSITY else "normal"
    workers = (workers or "disabled").strip()
    parsed_maxfail = None

    if workers not in {"disabled", "auto"}:
        try:
            parsed_workers = int(workers)
        except ValueError as exc:
            raise ValueError("xdist 进程数必须是 auto、disabled 或正整数") from exc
        if parsed_workers < 1 or parsed_workers > MAX_WORKERS:
            raise ValueError(f"xdist 进程数必须在 1 到 {MAX_WORKERS} 之间")
        workers = str(parsed_workers)

    tb = (tb or "auto").strip()
    if tb not in ALLOWED_TB_VALUES:
        raise ValueError("--tb 参数无效")

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
        last_failed=_parse_bool(last_failed),
        failed_first=_parse_bool(failed_first),
        tb=tb,
    )


def env_var_keys_from_text(raw: str) -> list[str]:
    return sorted(validate_env_vars(raw).keys())
