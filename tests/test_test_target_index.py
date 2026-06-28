import asyncio
import time
from pathlib import Path

from app import test_target_index
from app.projects import ProjectConfig
from app.test_target_index import TestTargetIndexCache as _TestTargetIndexCache, TestTargetIndexEntry as _TestTargetIndexEntry


def make_project(root: Path) -> ProjectConfig:
    return ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(root),
        python_executable="python3",
        working_directory=str(root),
        allowed_test_roots=[str(root)],
    )


def suggestion(value: str) -> dict:
    return {"value": value, "label": value, "kind": "file"}


def test_cache_first_request_schedules_rebuild_and_later_uses_cached_index(tmp_path, monkeypatch):
    calls = []

    def fake_build(project):
        calls.append(project.id)
        return [suggestion("tests/test_api.py")]

    async def run():
        cache = _TestTargetIndexCache(ttl_seconds=999)
        project = make_project(tmp_path)
        monkeypatch.setattr(test_target_index.discovery, "build_test_target_index", fake_build)

        assert await cache.get_suggestions(project, "api") == []
        await cache._entries[project.id].task

        assert await cache.get_suggestions(project, "api") == [suggestion("tests/test_api.py")]
        assert calls == ["demo"]

    asyncio.run(run())


def test_cache_deduplicates_concurrent_rebuilds(tmp_path, monkeypatch):
    calls = []

    def fake_build(project):
        calls.append(project.id)
        time.sleep(0.05)
        return [suggestion("tests/test_api.py")]

    async def run():
        cache = _TestTargetIndexCache(ttl_seconds=999)
        project = make_project(tmp_path)
        monkeypatch.setattr(test_target_index.discovery, "build_test_target_index", fake_build)

        await asyncio.gather(
            cache.get_suggestions(project, "api"),
            cache.get_suggestions(project, "api"),
            cache.get_suggestions(project, "api"),
        )
        await cache._entries[project.id].task

        assert calls == ["demo"]

    asyncio.run(run())


def test_cache_returns_stale_suggestions_while_rebuilding(tmp_path, monkeypatch):
    def fake_build(project):
        time.sleep(0.05)
        return [suggestion("tests/test_new.py")]

    async def run():
        cache = _TestTargetIndexCache(ttl_seconds=0)
        project = make_project(tmp_path)
        cache._entries[project.id] = _TestTargetIndexEntry(
            suggestions=[suggestion("tests/test_old.py")],
            status="ready",
            built_at=0,
            signature=cache._signature(project),
        )
        monkeypatch.setattr(test_target_index.discovery, "build_test_target_index", fake_build)

        assert await cache.get_suggestions(project, "test_") == [suggestion("tests/test_old.py")]
        await cache._entries[project.id].task
        assert await cache.get_suggestions(project, "new") == [suggestion("tests/test_new.py")]

    asyncio.run(run())


def test_cache_preserves_stale_suggestions_when_rebuild_fails(tmp_path, monkeypatch):
    def fake_build(project):
        raise RuntimeError("boom")

    async def run():
        cache = _TestTargetIndexCache(ttl_seconds=0)
        project = make_project(tmp_path)
        cache._entries[project.id] = _TestTargetIndexEntry(
            suggestions=[suggestion("tests/test_old.py")],
            status="ready",
            built_at=0,
            signature=cache._signature(project),
        )
        monkeypatch.setattr(test_target_index.discovery, "build_test_target_index", fake_build)

        assert await cache.get_suggestions(project, "old") == [suggestion("tests/test_old.py")]
        await cache._entries[project.id].task

        entry = cache._entries[project.id]
        assert entry.status == "failed"
        assert entry.suggestions == [suggestion("tests/test_old.py")]
        assert "RuntimeError" in entry.error

    asyncio.run(run())


def test_cache_invalidate_causes_next_request_to_rebuild(tmp_path, monkeypatch):
    calls = []

    def fake_build(project):
        calls.append(project.id)
        return [suggestion("tests/test_api.py")]

    async def run():
        cache = _TestTargetIndexCache(ttl_seconds=999)
        project = make_project(tmp_path)
        monkeypatch.setattr(test_target_index.discovery, "build_test_target_index", fake_build)

        await cache.get_suggestions(project, "api")
        await cache._entries[project.id].task
        cache.invalidate(project.id)

        assert await cache.get_suggestions(project, "api") == []
        await cache._entries[project.id].task
        assert calls == ["demo", "demo"]

    asyncio.run(run())
