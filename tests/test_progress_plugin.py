import json
from dataclasses import dataclass

from pytest_runner_platform_progress import plugin


@dataclass
class Report:
    nodeid: str
    when: str = "call"
    passed: bool = True
    failed: bool = False
    skipped: bool = False
    wasxfail: str | None = None


def read_progress(path):
    return json.loads(path.read_text(encoding="utf-8"))


def make_reporter(tmp_path, monkeypatch):
    current_time = {"value": 0.0}
    monkeypatch.setattr(plugin, "monotonic", lambda: current_time["value"])
    return plugin.ProgressReporter(tmp_path / "progress.json"), current_time


def finish_passed(reporter, nodeid):
    reporter.add_report(Report(nodeid=nodeid))
    reporter.finish(nodeid)


def test_finish_throttles_progress_writes(tmp_path, monkeypatch):
    reporter, current_time = make_reporter(tmp_path, monkeypatch)
    write_count = {"value": 0}
    original_write = reporter.write

    def counted_write():
        write_count["value"] += 1
        original_write()

    reporter.write = counted_write
    reporter.write()
    current_time["value"] = 0.1

    finish_passed(reporter, "test_one")
    finish_passed(reporter, "test_two")
    finish_passed(reporter, "test_three")

    assert write_count["value"] == 1
    assert reporter._dirty is True
    assert read_progress(reporter.path)["completed"] == 0


def test_finish_writes_again_after_interval(tmp_path, monkeypatch):
    reporter, current_time = make_reporter(tmp_path, monkeypatch)
    write_count = {"value": 0}
    original_write = reporter.write

    def counted_write():
        write_count["value"] += 1
        original_write()

    reporter.write = counted_write
    finish_passed(reporter, "test_one")
    assert write_count["value"] == 1

    current_time["value"] = plugin.PROGRESS_WRITE_INTERVAL_SECONDS - 0.01
    finish_passed(reporter, "test_two")
    assert write_count["value"] == 1

    current_time["value"] = plugin.PROGRESS_WRITE_INTERVAL_SECONDS + 0.01
    finish_passed(reporter, "test_three")
    assert write_count["value"] == 2
    assert read_progress(reporter.path)["completed"] == 3


def test_sessionfinish_flushes_dirty_progress(tmp_path, monkeypatch):
    reporter, current_time = make_reporter(tmp_path, monkeypatch)
    reporter.write()
    current_time["value"] = 0.1
    finish_passed(reporter, "test_one")

    assert read_progress(reporter.path)["completed"] == 0

    monkeypatch.setattr(plugin, "_REPORTER", reporter)
    plugin.pytest_sessionfinish(session=None, exitstatus=0)

    assert read_progress(reporter.path)["completed"] == 1
    assert reporter._dirty is False


def test_set_collected_writes_immediately(tmp_path, monkeypatch):
    reporter, _current_time = make_reporter(tmp_path, monkeypatch)

    reporter.set_collected(10)

    assert reporter.path.exists()
    assert read_progress(reporter.path)["collected"] == 10


def test_forced_flush_is_noop_when_not_dirty(tmp_path, monkeypatch):
    reporter, _current_time = make_reporter(tmp_path, monkeypatch)
    write_count = {"value": 0}
    original_write = reporter.write

    def counted_write():
        write_count["value"] += 1
        original_write()

    reporter.write = counted_write
    reporter.write()
    reporter.maybe_write(force=True)

    assert write_count["value"] == 1
