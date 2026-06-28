from __future__ import annotations

import ast
import os
from pathlib import Path

from .projects import ProjectConfig

IGNORED_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    "htmlcov",
}
MAX_SCAN_FILES = 1000
MAX_TEST_FILE_BYTES = 256 * 1024


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_allowed(path: Path, allowed_roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(_is_relative_to(resolved, root) for root in allowed_roots)


def _relative_value(project: ProjectConfig, path: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(project.root))
    except ValueError:
        return None


def _is_test_file(path: Path) -> bool:
    return path.is_file() and path.suffix == ".py" and (path.name.startswith("test_") or path.name.endswith("_test.py"))


def _is_test_function_node(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")


def _iter_discovery_paths(root: Path):
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in IGNORED_DIRS]
        current_path = Path(current_root)
        for dirname in dirnames:
            yield current_path / dirname
        for filename in filenames:
            yield current_path / filename


def _matches(value: str, label: str, query: str) -> bool:
    if not query:
        return True
    query = query.lower()
    return query in value.lower() or query in label.lower()


def _add_suggestion(
    suggestions: list[dict],
    seen: set[str],
    value: str,
    label: str,
    kind: str,
    query: str,
    limit: int,
) -> None:
    if len(suggestions) >= limit or value in seen or not _matches(value, label, query):
        return
    seen.add(value)
    suggestions.append({"value": value, "label": label, "kind": kind})


def _nodeid_suggestions(path: Path, relative_path: str) -> list[dict]:
    try:
        if path.stat().st_size > MAX_TEST_FILE_BYTES:
            return []
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, UnicodeError):
        return []

    suggestions: list[dict] = []
    for node in tree.body:
        if _is_test_function_node(node):
            suggestions.append({
                "value": f"{relative_path}::{node.name}",
                "label": node.name,
                "kind": "test",
            })
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if _is_test_function_node(child):
                    suggestions.append({
                        "value": f"{relative_path}::{node.name}::{child.name}",
                        "label": f"{node.name}::{child.name}",
                        "kind": "test",
                    })
    return suggestions


def build_test_target_index(project: ProjectConfig) -> list[dict]:
    allowed_roots = project.allowed_roots
    suggestions: list[dict] = []
    seen: set[str] = set()
    scanned_files = 0
    index_limit = 1_000_000

    for allowed_root in allowed_roots:
        if not _is_allowed(allowed_root, allowed_roots) or not allowed_root.exists():
            continue
        for path in _iter_discovery_paths(allowed_root):
            if scanned_files >= MAX_SCAN_FILES:
                break
            if path.is_dir():
                if not _is_allowed(path, allowed_roots):
                    continue
                relative = _relative_value(project, path)
                if relative:
                    _add_suggestion(suggestions, seen, relative, f"{relative}/", "directory", "", index_limit)
                continue
            if not _is_test_file(path) or not _is_allowed(path, allowed_roots):
                continue
            scanned_files += 1
            relative = _relative_value(project, path)
            if not relative:
                continue
            _add_suggestion(suggestions, seen, relative, relative, "file", "", index_limit)
            for suggestion in _nodeid_suggestions(path, relative):
                _add_suggestion(
                    suggestions,
                    seen,
                    suggestion["value"],
                    suggestion["label"],
                    suggestion["kind"],
                    "",
                    index_limit,
                )

    return suggestions


def filter_test_target_suggestions(suggestions: list[dict], query: str = "", limit: int = 50) -> list[dict]:
    query = query.strip()[:512]
    filtered: list[dict] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        _add_suggestion(
            filtered,
            seen,
            suggestion["value"],
            suggestion["label"],
            suggestion["kind"],
            query,
            limit,
        )
    return filtered


def list_test_target_suggestions(project: ProjectConfig, query: str = "", limit: int = 50) -> list[dict]:
    return filter_test_target_suggestions(build_test_target_index(project), query, limit)
