from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import discovery
from .projects import ProjectConfig


@dataclass
class TestTargetIndexEntry:
    suggestions: list[dict] = field(default_factory=list)
    status: str = "empty"
    built_at: float = 0.0
    signature: tuple = field(default_factory=tuple)
    error: str = ""
    task: asyncio.Task | None = None


class TestTargetIndexCache:
    def __init__(self, ttl_seconds: float = 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, TestTargetIndexEntry] = {}
        self._lock = asyncio.Lock()

    async def get_suggestions(self, project: ProjectConfig, query: str, limit: int = 50) -> list[dict]:
        signature = self._signature(project)
        async with self._lock:
            entry = self._entries.get(project.id)
            if entry is None:
                entry = TestTargetIndexEntry(status="building", signature=signature)
                self._entries[project.id] = entry
                self._schedule_rebuild(project, entry, signature)
                suggestions = []
            else:
                if self._needs_rebuild(entry, signature):
                    self._schedule_rebuild(project, entry, signature)
                suggestions = list(entry.suggestions)

        return discovery.filter_test_target_suggestions(suggestions, query, limit)

    def invalidate(self, project_id: str) -> None:
        self._entries.pop(project_id, None)

    def _needs_rebuild(self, entry: TestTargetIndexEntry, signature: tuple) -> bool:
        if entry.task and not entry.task.done():
            return False
        if entry.status in {"empty", "failed", "stale"}:
            return True
        if entry.signature != signature:
            return True
        return time.monotonic() - entry.built_at >= self.ttl_seconds

    def _schedule_rebuild(self, project: ProjectConfig, entry: TestTargetIndexEntry, signature: tuple) -> None:
        if entry.task and not entry.task.done():
            return
        entry.status = "building"
        entry.signature = signature
        entry.task = asyncio.create_task(self._rebuild(project, signature))

    async def _rebuild(self, project: ProjectConfig, signature: tuple) -> None:
        try:
            suggestions = await asyncio.to_thread(discovery.build_test_target_index, project)
        except Exception as exc:
            async with self._lock:
                entry = self._entries.setdefault(project.id, TestTargetIndexEntry(signature=signature))
                entry.status = "failed"
                entry.error = f"{type(exc).__name__}: {exc}"
                entry.built_at = time.monotonic()
            return

        async with self._lock:
            entry = self._entries.setdefault(project.id, TestTargetIndexEntry(signature=signature))
            entry.suggestions = suggestions
            entry.status = "ready"
            entry.error = ""
            entry.signature = signature
            entry.built_at = time.monotonic()

    def _signature(self, project: ProjectConfig) -> tuple:
        allowed_roots = tuple(str(Path(path).expanduser().resolve()) for path in project.allowed_test_roots)
        root_stats = []
        for root in allowed_roots:
            try:
                stat = Path(root).stat()
            except OSError:
                root_stats.append((root, None))
            else:
                root_stats.append((root, stat.st_mtime_ns))
        return (
            project.id,
            str(Path(project.root_path).expanduser().resolve()),
            allowed_roots,
            tuple(root_stats),
        )
