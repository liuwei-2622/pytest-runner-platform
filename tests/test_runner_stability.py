import asyncio
import signal
from pathlib import Path

from app import runner
from app.models import RunOptions, RunProgress, TestRun as _TestRun, utc_now
from app.projects import ProjectConfig


def make_project(tmp_path: Path, collect_timeout_seconds=20) -> ProjectConfig:
    return ProjectConfig(
        id="demo",
        name="Demo",
        root_path=str(tmp_path),
        python_executable="python3",
        working_directory=str(tmp_path),
        allowed_test_roots=[str(tmp_path)],
        collect_timeout_seconds=collect_timeout_seconds,
    )


def make_run(tmp_path: Path, run_id: str = "run123") -> _TestRun:
    report_dir = tmp_path / "reports" / run_id
    return _TestRun(
        id=run_id,
        status="queued",
        created_at=utc_now(),
        started_at=None,
        finished_at=None,
        test_path="tests",
        resolved_test_path=str(tmp_path),
        options=RunOptions(),
        return_code=None,
        command=[],
        report_dir=str(report_dir),
        html_report_path=str(report_dir / "pytest.html"),
        junit_report_path=str(report_dir / "junit.xml"),
        stdout_path=str(report_dir / "stdout.log"),
        stderr_path=str(report_dir / "stderr.log"),
        allure_results_path=str(report_dir / "allure-results"),
        allure_report_path=str(report_dir / "allure-report"),
        project_id="demo",
        project_name="Demo",
        progress=RunProgress(),
    )


class FakeCollectProcess:
    returncode = 0

    async def communicate(self):
        return b"collected 1 item", b""


class FakeSlowCollectProcess:
    returncode = None

    def __init__(self):
        self.calls = 0

    async def communicate(self):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1)
        return b"", b""


class FakeStreamCollectProcess:
    pid = 12345

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.returncode = None
        self._final_returncode = returncode
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self):
        await asyncio.sleep(0)
        self.returncode = self._final_returncode
        return self.returncode


class FakeHangingStreamCollectProcess:
    pid = 12345
    returncode = None

    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


class FakeRunProcess:
    def __init__(self, returncode=0):
        self.returncode = None
        self._final_returncode = returncode
        self.stdout = None
        self.stderr = None

    async def wait(self):
        self.returncode = self._final_returncode
        return self.returncode


class FakeHungProcess:
    pid = 12345
    returncode = None

    def __init__(self):
        self.wait_calls = 0

    async def wait(self):
        self.wait_calls += 1
        if self.wait_calls == 1:
            await asyncio.sleep(1)
        self.returncode = -9
        return self.returncode


def test_collect_tests_starts_pytest_in_process_group(tmp_path, monkeypatch):
    captured_kwargs = {}

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeCollectProcess()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(runner.collect_tests(make_project(tmp_path), tmp_path, RunOptions()))

    assert result["ok"] is True
    if runner.os.name == "posix":
        assert captured_kwargs["start_new_session"] is True


def test_terminate_process_group_sends_term_then_kill_after_grace_timeout(monkeypatch):
    calls = []

    def fake_killpg(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(runner.os, "killpg", fake_killpg)

    asyncio.run(runner._terminate_process_group(FakeHungProcess(), grace_seconds=0.001))

    assert calls == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]


def test_execute_run_starts_pytest_in_process_group(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    project = make_project(tmp_path)
    captured_kwargs = {}
    updates = []

    def fake_update_run(run_id, **changes):
        updates.append(changes)
        for key, value in changes.items():
            setattr(run, key, value)
        return run

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeRunProcess(returncode=0)

    async def noop_pump_stream(stream, path):
        return None

    async def noop_watch_progress(run_id, path, stop_event):
        return None

    async def noop_allure_report(run):
        return ""

    monkeypatch.setattr(runner, "get_run", lambda run_id: run)
    monkeypatch.setattr(runner, "get_project", lambda project_id: project)
    monkeypatch.setattr(runner, "update_run", fake_update_run)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(runner, "_pump_stream", noop_pump_stream)
    monkeypatch.setattr(runner, "_watch_progress", noop_watch_progress)
    monkeypatch.setattr(runner, "_generate_allure_report", noop_allure_report)

    asyncio.run(runner.execute_run(run.id))

    if runner.os.name == "posix":
        assert captured_kwargs["start_new_session"] is True
    assert updates[-1]["status"] == "passed"


def test_execute_run_marks_error_when_subprocess_start_raises(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    project = make_project(tmp_path)
    updates = []

    def fake_update_run(run_id, **changes):
        updates.append(changes)
        for key, value in changes.items():
            setattr(run, key, value)
        return run

    async def fake_create_subprocess_exec(*command, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "get_run", lambda run_id: run)
    monkeypatch.setattr(runner, "get_project", lambda project_id: project)
    monkeypatch.setattr(runner, "update_run", fake_update_run)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(runner.execute_run(run.id))

    assert updates[0]["status"] == "running"
    assert updates[-1]["status"] == "error"
    assert updates[-1]["finished_at"] is not None
    assert "RuntimeError" in updates[-1]["error_message"]
    assert "boom" in updates[-1]["error_message"]


def test_build_pytest_args_uses_unique_progress_plugin_name(tmp_path):
    command = runner.build_pytest_args(
        project=make_project(tmp_path),
        test_path=str(tmp_path),
        options=RunOptions(),
        report_paths=None,
    )

    assert runner.PROGRESS_PLUGIN == "pytest_runner_platform_progress.plugin"
    assert command[command.index("-p") + 1] == runner.PROGRESS_PLUGIN
    assert "app.pytest_progress" not in command


def test_unique_progress_plugin_is_importable():
    import pytest_runner_platform_progress.plugin as plugin

    assert hasattr(plugin, "pytest_configure")


def test_collect_command_excludes_progress_plugin_and_report_args(tmp_path, monkeypatch):
    captured_command = []

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured_command.extend(command)
        return FakeCollectProcess()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(runner.collect_tests(make_project(tmp_path), tmp_path, RunOptions()))

    assert "--collect-only" in captured_command
    assert captured_command[captured_command.index("-o") + 1] == "addopts="
    assert runner.PROGRESS_PLUGIN not in captured_command
    assert not any(item.startswith("--html=") for item in captured_command)
    assert "--self-contained-html" not in captured_command
    assert not any(item.startswith("--junitxml=") for item in captured_command)
    assert not any(item.startswith("--alluredir=") for item in captured_command)


def test_build_pytest_args_can_degrade_to_junit_only(tmp_path):
    report_paths = {
        "html": str(tmp_path / "pytest.html"),
        "junit": str(tmp_path / "junit.xml"),
        "allure": str(tmp_path / "allure-results"),
    }

    command = runner.build_pytest_args(
        project=make_project(tmp_path),
        test_path=str(tmp_path),
        options=RunOptions(),
        report_paths=report_paths,
        report_plugins={"html": False, "allure": False},
    )

    assert f"--junitxml={report_paths['junit']}" in command
    assert not any(item.startswith("--html=") for item in command)
    assert "--self-contained-html" not in command
    assert not any(item.startswith("--alluredir=") for item in command)


def test_build_pytest_args_keeps_optional_report_flags_when_available(tmp_path):
    report_paths = {
        "html": str(tmp_path / "pytest.html"),
        "junit": str(tmp_path / "junit.xml"),
        "allure": str(tmp_path / "allure-results"),
    }

    command = runner.build_pytest_args(
        project=make_project(tmp_path),
        test_path=str(tmp_path),
        options=RunOptions(),
        report_paths=report_paths,
        report_plugins={"html": True, "allure": True},
    )

    assert f"--html={report_paths['html']}" in command
    assert "--self-contained-html" in command
    assert f"--junitxml={report_paths['junit']}" in command
    assert f"--alluredir={report_paths['allure']}" in command


def test_report_plugin_flags_auto_checks_target_modules(tmp_path, monkeypatch):
    calls = []

    def fake_available(project, options, module_name):
        calls.append(module_name)
        return module_name == "pytest_html"

    monkeypatch.setattr(runner, "REPORT_PLUGIN_MODE", "auto")
    monkeypatch.setattr(runner, "_target_module_available", fake_available)

    flags = runner._report_plugin_flags(make_project(tmp_path), RunOptions())

    assert flags == {"html": True, "allure": False}
    assert calls == ["pytest_html", "allure_pytest"]


def test_collect_tests_returns_structured_error_when_subprocess_start_fails(tmp_path, monkeypatch):
    async def fake_create_subprocess_exec(*command, **kwargs):
        raise FileNotFoundError("missing-python")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(runner.collect_tests(make_project(tmp_path), tmp_path, RunOptions()))

    assert result["ok"] is False
    assert result["return_code"] is None
    assert result["command"]
    assert result["display_command"]
    assert result["collected_count"] is None
    assert result["stdout"] == ""
    assert "FileNotFoundError" in result["stderr"]
    assert "missing-python" in result["stderr"]
    assert result["timed_out"] is False
    assert result["error"] == result["stderr"]


def test_collect_tests_uses_project_timeout(tmp_path, monkeypatch):
    async def fake_create_subprocess_exec(*command, **kwargs):
        return FakeSlowCollectProcess()

    async def fake_terminate(process):
        process.returncode = -9

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(runner, "_terminate_process_group", fake_terminate)

    result = asyncio.run(runner.collect_tests(make_project(tmp_path, collect_timeout_seconds=0.001), tmp_path, RunOptions()))

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert "0.001" in result["error"]
    assert "Collect timed out after 0.001 seconds" in result["stderr"]


def test_stream_collect_tests_emits_output_and_complete_events(tmp_path, monkeypatch):
    async def fake_create_subprocess_exec(*command, **kwargs):
        return FakeStreamCollectProcess(b"collected 2 items\n", b"warning\n")

    async def collect_events():
        monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        return [event async for event in runner.stream_collect_tests(make_project(tmp_path), tmp_path, RunOptions())]

    events = asyncio.run(collect_events())

    assert [event["event"] for event in events] == ["start", "stdout", "stderr", "complete"]
    assert events[0]["timeout_seconds"] == 20
    assert events[1]["text"] == "collected 2 items\n"
    assert events[2]["text"] == "warning\n"
    assert events[-1]["ok"] is True
    assert events[-1]["collected_count"] == 2


def test_stream_collect_tests_emits_timeout_event(tmp_path, monkeypatch):
    async def fake_terminate(target):
        target.returncode = -9
        target.stdout.feed_eof()
        target.stderr.feed_eof()

    async def collect_events():
        process = FakeHangingStreamCollectProcess()

        async def fake_create_subprocess_exec(*command, **kwargs):
            return process

        monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(runner, "_terminate_process_group", fake_terminate)
        return [event async for event in runner.stream_collect_tests(make_project(tmp_path, collect_timeout_seconds=0), tmp_path, RunOptions())]

    events = asyncio.run(collect_events())

    assert events[0]["event"] == "start"
    assert events[-2]["event"] == "stderr"
    assert "Collect timed out after 0 seconds" in events[-2]["text"]
    assert events[-1]["event"] == "complete"
    assert events[-1]["timed_out"] is True
    assert events[-1]["ok"] is False


def test_quote_command_for_display_quotes_spaces_and_shell_chars():
    command = ["python", "-m", "pytest", "/tmp/project with spaces/tests", "-k", "name and not slow"]

    assert runner.quote_command_for_display(command) == (
        "python -m pytest '/tmp/project with spaces/tests' -k 'name and not slow'"
    )
