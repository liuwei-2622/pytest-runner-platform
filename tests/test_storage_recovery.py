from app import storage
from app.models import RunOptions, utc_now


def isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(storage, "RUN_METADATA_DB", tmp_path / "runs.sqlite3")
    storage._initialized_storage.clear()


def test_recover_stale_runs_marks_queued_and_running_as_error(tmp_path, monkeypatch):
    isolate_storage(tmp_path, monkeypatch)

    queued = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    running = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())
    passed = storage.create_run("demo", "Demo", "tests", tmp_path / "tests", RunOptions())

    storage.update_run(running.id, status="running", started_at=utc_now())
    storage.update_run(passed.id, status="passed", started_at=utc_now(), finished_at=utc_now(), return_code=0)

    count = storage.recover_stale_runs("recovered after restart")

    recovered_queued = storage.get_run(queued.id)
    recovered_running = storage.get_run(running.id)
    unchanged_passed = storage.get_run(passed.id)

    assert count == 2
    assert recovered_queued.status == "error"
    assert recovered_queued.finished_at is not None
    assert recovered_queued.error_message == "recovered after restart"
    assert recovered_running.status == "error"
    assert recovered_running.finished_at is not None
    assert recovered_running.error_message == "recovered after restart"
    assert unchanged_passed.status == "passed"
    assert unchanged_passed.error_message == ""
